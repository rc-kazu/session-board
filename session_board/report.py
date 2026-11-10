"""その日の作業報告: 事実の抽出・プロンプト生成・応答の検証・テキスト整形・キャッシュ (純粋関数中心)。

ファイルや git を読む部分は collectors.py にある。ここはそれらの結果を受け取って組み立てる。
"""

import datetime
import hashlib
import json
import os
import re
import tempfile

from .classify import resolve_path
from .llm import WRITING_RULES
from .phase import parse_iso
from .transcript import _EDIT_TOOLS, _NOTABLE_CMD_RE, PR_URL_RE, _tool_result_text, _user_text

OTHER = "その他"  # 案件名が決まらないときの既定 (追跡中の案件は classifier.other_label を使う)
STATUSES = ("予定通り", "やや遅れ", "遅れ")
DEFAULT_STATUS = "予定通り"
SECTIONS = [
    ("status", "今日の進み具合"),
    ("done", "今日終わったこと"),
    ("carryover", "残っていること"),
    ("tomorrow", "明日やること"),
    ("share", "共有・相談したいこと"),
]
GROUP_KEYS = ("done", "carryover", "tomorrow", "share")
OPEN_PHASES = ("調査", "実装中", "レビュー待ち")

MAX_PROMPT_CHARS = 600
MAX_ASSISTANT_CHARS = 800
MAX_ASSISTANT_KEEP = 3
MAX_DAY_FILES = 60
MAX_DAY_COMMANDS = 10
DEFAULT_MAX_CHARS = 12000

LIMIT_PROJECT = 40
LIMIT_TEXT = 120
LIMIT_GROUPS = 20
LIMIT_ITEMS = 30
LIMIT_CHILDREN = 12

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WORKTREE_SPLIT = ("/.claude/worktrees/", "/.worktrees/")
_TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def _clip(text, n):
    text = (text or "").replace("\r", "").strip()
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


# ---------------------------------------------------------------- 日付

def parse_date(value):
    """'YYYY-MM-DD' を date にする。形式・日付として不正なら ValueError。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError("日付は YYYY-MM-DD で指定してください")
    return datetime.date.fromisoformat(value)


def today():
    return datetime.date.today()


def day_bounds(date, tz=None):
    """その日の [開始, 終了) を epoch 秒で返す。tz 省略時はシステムのローカル時刻 (夏時間も考慮)。"""
    nxt = date + datetime.timedelta(days=1)
    if tz is None:
        start = datetime.datetime(date.year, date.month, date.day).astimezone()
        end = datetime.datetime(nxt.year, nxt.month, nxt.day).astimezone()
    else:
        start = datetime.datetime(date.year, date.month, date.day, tzinfo=tz)
        end = datetime.datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def _in_range(ts, start, end):
    t = parse_iso(ts)
    return t is not None and start <= t < end


# ---------------------------------------------------------------- セッション (1 日分)

def parse_day_lines(lines, start, end):
    """トランスクリプトの行から [start, end) のやりとりだけを抜き出す。その日に何も無ければ None。"""
    s = {
        "session_id": None,
        "cwd": None,
        "branch": None,
        "ai_title": None,
        "prompts": [],
        "assistant": [],
        "files": [],
        "prs_created": [],
        "commands": [],
        "first_ts": None,
        "last_ts": None,
    }
    activity = 0
    files_seen = set()
    pr_create_ids = set()
    fallback = (None, None)

    for raw in lines:
        if not raw or not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if s["session_id"] is None and d.get("sessionId"):
            s["session_id"] = d.get("sessionId")
        if t == "ai-title":
            s["ai_title"] = d.get("aiTitle") or s["ai_title"]
            continue
        if d.get("isSidechain") or t not in ("user", "assistant"):
            continue
        if d.get("cwd"):
            fallback = (d.get("cwd"), d.get("gitBranch") or None)
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if t == "assistant" and isinstance(content, list):
            # gh pr create の結果は日付をまたぐことがあるので、ID は範囲外でも覚える
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                    if "gh pr create" in ((c.get("input") or {}).get("command") or ""):
                        pr_create_ids.add(c.get("id"))
        ts = d.get("timestamp")
        if not _in_range(ts, start, end):
            continue
        activity += 1
        if s["first_ts"] is None or ts < s["first_ts"]:
            s["first_ts"] = ts
        if s["last_ts"] is None or ts > s["last_ts"]:
            s["last_ts"] = ts
        if d.get("cwd"):
            s["cwd"] = d.get("cwd")
            s["branch"] = d.get("gitBranch") or None
        if t == "user":
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pr_create_ids:
                        for m in PR_URL_RE.finditer(_tool_result_text(c)):
                            if m.group(0) not in s["prs_created"]:
                                s["prs_created"].append(m.group(0))
            if d.get("isMeta"):
                continue
            text = _user_text(content)
            if text is not None:
                s["prompts"].append({"ts": ts, "text": _clip(text, MAX_PROMPT_CHARS)})
        elif isinstance(content, list):
            texts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    texts.append(c.get("text") or "")
                elif c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    if c.get("name") in _EDIT_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp and fp not in files_seen and len(s["files"]) < MAX_DAY_FILES:
                            files_seen.add(fp)
                            s["files"].append(fp)
                    elif c.get("name") == "Bash":
                        cmd = inp.get("command") or ""
                        if _NOTABLE_CMD_RE.search(cmd) and len(s["commands"]) < MAX_DAY_COMMANDS:
                            s["commands"].append(_clip(cmd, 200))
            text = "\n".join(x for x in texts if x).strip()
            if text:
                s["assistant"].append(_clip(text, MAX_ASSISTANT_CHARS))
                s["assistant"] = s["assistant"][-MAX_ASSISTANT_KEEP:]
    if activity == 0:
        return None
    if not s["cwd"]:
        s["cwd"], s["branch"] = fallback
    return s


def _strip_worktree(path):
    for marker in _WORKTREE_SPLIT:
        i = path.find(marker)
        if i > 0:
            return path[:i]
    return path


def project_name(cwd, files, branch, classifier, repo_paths, worktrees, root=None):
    """セッションの案件名。対象 (targets.json) > ブランチのキーワード > 追跡リポジトリ名 > ディレクトリ名。"""
    hits = {}
    for p in ([cwd] if cwd else []) + list(files or []):
        res = resolve_path(p, repo_paths, worktrees)
        if not res:
            continue
        for target, _kind in classifier.classify(res["repo"], res["rel"]):
            hits[target] = hits.get(target, 0) + 1
    if hits:
        return max(hits.items(), key=lambda kv: kv[1])[0]
    kw = classifier.keyword_targets(branch or "")
    if kw:
        return kw[0]
    other = getattr(classifier, "other_label", OTHER)
    if not cwd:
        return other
    res = resolve_path(cwd, repo_paths, worktrees)
    if res:
        return res["repo"]
    if cwd.startswith(_TMP_PREFIXES):
        return other
    path = _strip_worktree(cwd.rstrip("/"))
    if root and (path == root or path.startswith(root.rstrip("/") + "/")):
        path = root
    if path.rstrip("/") == os.path.expanduser("~").rstrip("/"):
        return other
    return os.path.basename(path.rstrip("/")) or other


# ---------------------------------------------------------------- PR / コミット

def _pr_project(p, classifier):
    hits = {}
    for f in p.get("files") or []:
        path = f.get("path") if isinstance(f, dict) else f
        for target, _kind in classifier.classify(p.get("repo"), path):
            hits[target] = hits.get(target, 0) + 1
    if hits:
        return max(hits.items(), key=lambda kv: kv[1])[0]
    kw = classifier.keyword_targets((p.get("headRefName") or "") + " " + (p.get("title") or ""))
    return kw[0] if kw else (p.get("repo") or getattr(classifier, "other_label", OTHER))


def prs_for_day(prs, start, end, classifier):
    """その日に作成・マージ・クローズ・更新された PR。"""
    out = []
    for p in prs or []:
        events = []
        if _in_range(p.get("createdAt"), start, end):
            events.append("作成")
        if _in_range(p.get("mergedAt"), start, end):
            events.append("マージ")
        elif p.get("state") == "CLOSED" and _in_range(p.get("closedAt") or p.get("updatedAt"), start, end):
            events.append("クローズ")
        if not events and _in_range(p.get("updatedAt"), start, end):
            events.append("更新")
        if not events:
            continue
        out.append({
            "repo": p.get("repo"),
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "isDraft": bool(p.get("isDraft")),
            "reviewDecision": p.get("reviewDecision") or "",
            "events": events,
            "project": _pr_project(p, classifier),
        })
    return out


def parse_git_log(text, start, end):
    """`git log --format=%x1e%H%x1f%aI%x1f%s --name-only` を解析し、author 日時が範囲内のものだけ返す。"""
    out = []
    for rec in (text or "").split("\x1e"):
        if not rec.strip():
            continue
        head, _, rest = rec.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 3:
            continue
        h, ts, subject = parts[0].strip(), parts[1].strip(), "\x1f".join(parts[2:]).strip()
        if not _in_range(ts, start, end):
            continue
        files = [x.strip() for x in rest.splitlines() if x.strip()]
        out.append({"hash": h, "ts": ts, "subject": subject, "files": files})
    return out


def commit_project(repo, files, classifier):
    hits = {}
    for f in files or []:
        for target, _kind in classifier.classify(repo, f):
            hits[target] = hits.get(target, 0) + 1
    if hits:
        return max(hits.items(), key=lambda kv: kv[1])[0]
    return repo


# ---------------------------------------------------------------- 案件

def active_items(items, end, days=7, limit=20, other=OTHER):
    """進行中の案件 (残っていること・明日やることの推定材料)。最近動いたもの、またはレビュー待ちのもの。"""
    out = []
    for it in items or []:
        if it.get("hidden") or it.get("phase") not in OPEN_PHASES:
            continue
        t = parse_iso(it.get("activity_ts") or it.get("last_activity"))
        recent = t is not None and end - days * 86400 <= t < end
        if not recent and it.get("phase") != "レビュー待ち":
            continue
        project = it.get("target") or other
        if project == other and it.get("repo_branches"):
            project = it["repo_branches"][0].get("repo") or other
        out.append({
            "project": project,
            "title": it.get("title") or "",
            "phase": it.get("phase"),
            "stale": bool(it.get("stale")),
            "next_action": it.get("next_action") or "",
            "changes_requested": bool(it.get("changes_requested")),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 事実 -> プロンプト

def build_facts(date, sessions, prs, commits, items):
    return {
        "date": date,
        "sessions": sorted(sessions or [], key=lambda s: (s.get("project") or "", s.get("first_ts") or "")),
        "prs": list(prs or []),
        "commits": list(commits or []),
        "items": list(items or []),
    }


# 予算に収まるまで段階的に削る。依頼・PR・コミットを優先し、Claude の返答やファイルから先に削る
_LEVELS = [
    {"prompt": 300, "prompts": 12, "asst": 2, "asst_chars": 400, "files": 10, "cmds": 4},
    {"prompt": 220, "prompts": 10, "asst": 1, "asst_chars": 300, "files": 6, "cmds": 3},
    {"prompt": 160, "prompts": 8, "asst": 1, "asst_chars": 180, "files": 4, "cmds": 2},
    {"prompt": 120, "prompts": 6, "asst": 0, "asst_chars": 0, "files": 3, "cmds": 1},
    {"prompt": 100, "prompts": 4, "asst": 0, "asst_chars": 0, "files": 0, "cmds": 0},
    {"prompt": 80, "prompts": 3, "asst": 0, "asst_chars": 0, "files": 0, "cmds": 0},
    {"prompt": 60, "prompts": 2, "asst": 0, "asst_chars": 0, "files": 0, "cmds": 0},
]


def _one_line(text, n):
    return _clip(re.sub(r"\s+", " ", text or ""), n)


def _pick_prompts(prompts, n):
    if len(prompts) <= n:
        return prompts
    head = max(1, n // 3)
    return prompts[:head] + prompts[len(prompts) - (n - head):]


def _short_path(p):
    home = os.path.expanduser("~")
    if p and (p == home or p.startswith(home + "/")):
        return "~" + p[len(home):]
    return p or "?"


def _local_hm(ts):
    t = parse_iso(ts)
    if t is None:
        return ""
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def _head_lines(facts):
    lines = ["対象日: %s" % facts["date"]]
    if facts["prs"]:
        lines.append("## この日に動いた PR")
        for p in facts["prs"][:30]:
            state = p.get("state") or "?"
            if p.get("isDraft"):
                state += " ドラフト"
            if p.get("reviewDecision") == "CHANGES_REQUESTED":
                state += " 修正依頼あり"
            lines.append("- [%s] %s 「%s」 この日: %s / 現在: %s" % (
                p.get("project"), p.get("repo"), _one_line(p.get("title") or "(タイトル不明)", 100),
                "・".join(p.get("events") or []), state))
    if facts["commits"]:
        lines.append("## この日のコミット")
        for c in facts["commits"][:30]:
            lines.append("- [%s] %s: %s" % (c.get("project"), c.get("repo"), _one_line(c.get("subject"), 100)))
    return lines


def _session_lines(facts, lv):
    lines = []
    seen = {}
    for s in facts["sessions"]:
        project = s.get("project") or OTHER
        seen_p = seen.setdefault(project, set())
        prompts = []
        for p in s.get("prompts") or []:
            key = re.sub(r"\s+", " ", p.get("text") or "").strip()
            if not key or key in seen_p:
                continue
            seen_p.add(key)
            prompts.append(p)
        prompts = _pick_prompts(prompts, lv["prompts"])
        asst = (s.get("assistant") or [])[-lv["asst"]:] if lv["asst"] else []
        files = (s.get("files") or [])[: lv["files"]]
        cmds = (s.get("commands") or [])[: lv["cmds"]]
        prs = s.get("prs_created") or []
        if not (prompts or asst or files or prs):
            continue
        head = "### [%s] %s" % (project, _short_path(s.get("cwd")))
        if s.get("branch"):
            head += " (ブランチ %s)" % s["branch"]
        if s.get("ai_title"):
            head += " 自動タイトル: " + _one_line(s["ai_title"], 60)
        lines.append(head)
        if prompts:
            lines.append("依頼:")
            for p in prompts:
                lines.append("- %s %s" % (_local_hm(p.get("ts")), _one_line(p.get("text"), lv["prompt"])))
        for url in prs[:5]:
            m = PR_URL_RE.search(url)
            lines.append("作成した PR: %s" % ("%s #%s" % (m.group(2), m.group(3)) if m else url))
        if cmds:
            lines.append("コマンド: " + " / ".join(_one_line(c, 80) for c in cmds))
        if files:
            lines.append("編集: " + ", ".join(os.path.basename(f) for f in files))
        for a in asst:
            lines.append("Claude の返答: " + _one_line(a, lv["asst_chars"]))
    if lines:
        lines.insert(0, "## この日の Claude Code セッション (依頼は時刻順)")
    return lines


def _item_lines(facts):
    if not facts["items"]:
        return []
    lines = ["## 進行中の案件 (参考: 残っていること・明日やることの推定用)"]
    for it in facts["items"]:
        phase = it.get("phase") or "?"
        if it.get("stale"):
            phase += "・放置"
        if it.get("changes_requested"):
            phase += "・修正依頼あり"
        line = "- [%s] %s / %s" % (it.get("project"), _one_line(it.get("title"), 60), phase)
        if it.get("next_action"):
            line += " / 次: " + _one_line(it["next_action"], 100)
        lines.append(line)
    return lines


def facts_text(facts, max_chars=DEFAULT_MAX_CHARS):
    """LLM に渡す事実のテキスト。max_chars 以内に収める。"""
    head = "\n".join(_head_lines(facts))
    items = "\n".join(_item_lines(facts))
    text = ""
    for lv in _LEVELS:
        parts = [head, "\n".join(_session_lines(facts, lv)), items]
        text = "\n".join(x for x in parts if x)
        if len(text) <= max_chars:
            return text
    return _clip(text, max_chars)


def facts_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


REPORT_SYSTEM_PROMPT = (
    "あなたは開発者の 1 日の作業記録から作業報告を作るアシスタントです。"
    "出力は指定された形の JSON オブジェクト 1 つだけにしてください。前置きやコードフェンスは不要です。\n\n"
    + WRITING_RULES
)
REPORT_SCHEMA = (
    '{"status": "予定通り|やや遅れ|遅れ のいずれか", '
    '"done": [{"project": "案件名", "items": [{"text": "作業 (1 行)", "children": ["細かい手順 (任意)"]}]}], '
    '"carryover": [同じ形], "tomorrow": [同じ形], "share": [同じ形]}'
)


def build_report_prompt(date, text):
    rules = [
        "次の事実から %s の作業報告 を作ってください。" % date,
        "",
        "## 書き方",
        "- done (今日終わったこと): この日に終えたこと。マージした PR、作成した PR、結論が出た調査、反映した設定など",
        "- carryover (残っていること): この日に手を付けたが終わっていないこと、レビュー待ち・修正待ちのもの",
        "- tomorrow (明日やること): 持ち越しと、進行中の案件の次にやることから自然に続く作業",
        "- share (共有・相談したいこと): 他の人の判断・回答が必要な事項や障害が事実に明記されているときだけ。無ければ空配列",
        "- status: 遅れの兆候が事実に無ければ「予定通り」",
        "- 各行は 1 つの作業項目。何に取り組んだか / 何をしたかを、体言止めの短い日本語 (30 文字程度まで) で書く"
        " (例: 「ログイン画面の文言修正」「検索 API のページング追加」「依存パッケージの更新」)",
        "- 「〜のPR作成」「〜のレビュー対応」のように手順を書くのはよいが、PR 番号・URL・リンク・ブランチ名・セッション ID は"
        "どの行にも絶対に書かない。行の中で project 名を繰り返さない",
        "- PR・コミット・セッションは作業を推定する材料にすぎない。コミットを 1 件ずつ並べない。"
        "同じ作業に属する複数の PR・コミット・セッション (環境違いの同じ変更など) は 1 行にまとめる",
        "- children は 1 つの作業に目立つ段階が複数あるときだけ、短く 3 つまで。無ければ空配列",
        "- 1 つの project の done はおおむね 6 行以内。細かい修正はまとめて 1 行にする",
        "- マージされずにクローズされた PR は完了扱いにしない (必要なら触れない)",
        "- この日に作成した PR は、レビュー待ちでも done に「〜のPR作成」として書き、carryover には「〜のレビュー対応」として書く",
        "- 事実に無い作業を作らない。重複はまとめる。絵文字は使わない",
        "",
        "## 事実",
        text,
        "",
        "出力形式 (JSON のみ): " + REPORT_SCHEMA,
    ]
    return "\n".join(rules)


# ---------------------------------------------------------------- 応答の検証

_URL_RE = re.compile(r"https?://\S+")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PR_REF_RE = re.compile(r"(?:\b(?:PR|pr|Pull Request|pull request|プルリク(?:エスト)?)\s*#\s*\d+"
                        r"|\b(?:PR|pr)\s+\d+(?![0-9件本個つ])"
                        r"|#\d+\b)")
_EMPTY_PARENS_RE = re.compile(r"\s*(?:\(\s*\)|（\s*）|\[\s*\]|「\s*」)")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def _remove(pattern, text):
    def repl(m):
        s, e = m.span()
        left = text[s - 1] if s > 0 else ""
        right = text[e] if e < len(text) else ""
        return " " if _ASCII_WORD.match(left) and _ASCII_WORD.match(right) else ""

    spaced = re.compile(r"\s*(?:" + pattern.pattern + r")\s*")
    return spaced.sub(repl, text)


def sanitize_line(text):
    """PR 番号・URL・セッション ID を取り除く (作業報告に出さない)。"""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    for rx in (_URL_RE, _UUID_RE, _PR_REF_RE):
        text = _remove(rx, text)
    text = _EMPTY_PARENS_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.strip("、,・/ ")
    return text


def _norm_items(raw):
    out = []
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, str):
            text, children = x, []
        elif isinstance(x, dict):
            text = x.get("text") if isinstance(x.get("text"), str) else ""
            ch = x.get("children")
            children = [ch] if isinstance(ch, str) else (ch if isinstance(ch, list) else [])
        else:
            continue
        text = _clip(sanitize_line(text), LIMIT_TEXT)
        kids = []
        for c in children:
            if isinstance(c, str):
                c = _clip(sanitize_line(c), LIMIT_TEXT)
                if c and c not in kids:
                    kids.append(c)
        if not text:
            continue
        out.append({"text": text, "children": kids[:LIMIT_CHILDREN]})
        if len(out) >= LIMIT_ITEMS:
            break
    return out


def _norm_groups(raw):
    groups = []
    index = {}
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        project = g.get("project") if isinstance(g.get("project"), str) else ""
        project = _clip(sanitize_line(project), LIMIT_PROJECT) or OTHER
        items = _norm_items(g.get("items"))
        if not items:
            continue
        if project in index:
            index[project]["items"].extend(items)
            continue
        rec = {"project": project, "items": items}
        index[project] = rec
        groups.append(rec)
        if len(groups) >= LIMIT_GROUPS:
            break
    return groups


def _result_text(stdout):
    """claude -p --output-format json の封筒から本文を取り出す。エラーなら None。"""
    try:
        env = json.loads(stdout)
    except ValueError:
        return stdout
    if isinstance(env, dict) and "result" in env:
        if env.get("is_error"):
            return None
        return env.get("result") if isinstance(env.get("result"), str) else None
    return stdout


def envelope_meta(stdout):
    """claude -p の封筒から費用・所要時間・出力トークン数を取り出す (記録用)。"""
    try:
        env = json.loads(stdout)
    except (ValueError, TypeError):
        return {}
    if not isinstance(env, dict):
        return {}
    out = {}
    if isinstance(env.get("total_cost_usd"), (int, float)):
        out["cost_usd"] = env["total_cost_usd"]
    if isinstance(env.get("duration_ms"), (int, float)):
        out["duration_ms"] = env["duration_ms"]
    usage = env.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
        out["output_tokens"] = usage["output_tokens"]
    return out


def parse_report_output(stdout):
    """LLM の出力を検証して報告 dict にする。解釈できなければ None。"""
    if not stdout or not stdout.strip():
        return None
    text = _result_text(stdout)
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    status = obj.get("status")
    out = {"status": status if status in STATUSES else DEFAULT_STATUS}
    for k in GROUP_KEYS:
        out[k] = _norm_groups(obj.get(k))
    return out


# ---------------------------------------------------------------- 整形

def render_groups(groups):
    lines = []
    for g in groups or []:
        lines.append(g["project"])
        for it in g.get("items") or []:
            lines.append("• " + it["text"])
            for c in it.get("children") or []:
                lines.append("    ◦ " + c)
    return "\n".join(lines)


def render_texts(result):
    out = {"status": (result or {}).get("status") or DEFAULT_STATUS}
    for k in GROUP_KEYS:
        out[k] = render_groups((result or {}).get(k))
    return out


def render_full(texts):
    lines = []
    for key, label in SECTIONS:
        body = (texts.get(key) or "").rstrip()
        if key == "status":
            lines.append("%s: %s" % (label, body))
        else:
            lines.append(label + ":")
            if body:
                lines.append(body)
    return "\n".join(lines)


# ---------------------------------------------------------------- キャッシュ

def report_path(data_dir, date):
    parse_date(date)
    return os.path.join(data_dir, "reports", date + ".json")


def load_report(data_dir, date):
    path = report_path(data_dir, date)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("result"), dict):
        return None
    return d


def save_report(data_dir, date, fhash, result, seconds, generated_at, meta=None):
    path = report_path(data_dir, date)
    rec = {"date": date, "facts_hash": fhash, "result": result, "seconds": seconds, "generated_at": generated_at,
           "llm": meta or {}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec
