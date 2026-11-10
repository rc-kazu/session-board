"""Claude Code のトランスクリプト (jsonl) から事実を抽出する純粋ロジック。"""

import json
import re

PR_URL_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")
_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_TAG_PREFIXES = (
    "<local-command",
    "<system-reminder",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<task-notification",
    "Caveat:",
)
_NOISE_PREFIXES = ("[Request interrupted", "[Request cancelled")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_NOTABLE_CMD_RE = re.compile(r"\b(gh pr (create|merge|ready|edit)|git push|git checkout -b|git switch -c|git worktree add)\b")
# gh pr create をコマンドとして実行したときだけ。文字列として含むだけのコマンド (grep やスクリプトの中) は数えない
_PR_CREATE_RE = re.compile(r"(?:^|[;&|(\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*gh\s+pr\s+create\b")
_SKIP_SLASH = {"/clear", "/compact", "/exit", "/resume", "/model", "/status", "/cost", "/help", "/login"}

MAX_FIRST = 3
MAX_RECENT = 8
MAX_TEXT = 600
MAX_COMMANDS = 10
MAX_EDITS = 300


def parse_pr_url(url):
    m = PR_URL_RE.search(url or "")
    if not m:
        return None
    return (m.group(1), m.group(2), int(m.group(3)))


def _clip(text, n=MAX_TEXT):
    text = (text or "").strip()
    if len(text) > n:
        return text[: n - 1] + "…"
    return text


def _user_text(content):
    """ユーザー発話として意味のあるテキストを返す。該当しなければ None。"""
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result":
                return None
            if c.get("type") == "text":
                texts.append(c.get("text") or "")
    else:
        return None
    text = "\n".join(t for t in texts if t).strip()
    if not text:
        return None
    if text.startswith(_NOISE_PREFIXES):
        return None
    m = _CMD_NAME_RE.search(text)
    if m:
        name = m.group(1).strip()
        am = _CMD_ARGS_RE.search(text)
        args = am.group(1).strip() if am else ""
        if name in _SKIP_SLASH or not args:
            return None
        return (name + " " + args).strip()
    if text.startswith(_TAG_PREFIXES):
        return None
    return text


def _tool_result_text(c):
    content = c.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def parse_lines(lines):
    s = {
        "session_id": None,
        "first_ts": None,
        "last_ts": None,
        "cwd_last": None,
        "branch_last": None,
        "locations": [],
        "first_prompts": [],
        "last_prompt": None,
        "recent": [],
        "ai_title": None,
        "files_edited": [],
        "edits": [],
        "prs": [],
        "commands": [],
        "n_user": 0,
        "n_assistant": 0,
    }
    loc_index = {}
    files_seen = set()
    edits_seen = set()
    pr_index = {}
    pr_create_ids = set()

    def add_pr(url, created):
        p = parse_pr_url(url)
        if not p:
            return
        key = (p[1], p[2])
        if key in pr_index:
            if created:
                pr_index[key]["created"] = True
            return
        rec = {"org": p[0], "repo": p[1], "number": p[2], "url": "https://github.com/%s/%s/pull/%d" % p, "created": created}
        pr_index[key] = rec
        s["prs"].append(rec)

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
        if d.get("isSidechain"):
            continue
        ts = d.get("timestamp")
        cwd = d.get("cwd")
        if ts and cwd:
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if t in ("user", "assistant") and (s["last_ts"] is None or ts > s["last_ts"]):
                s["last_ts"] = ts
            branch = d.get("gitBranch") or None
            s["cwd_last"] = cwd
            s["branch_last"] = branch
            key = (cwd, branch)
            if key not in loc_index:
                loc_index[key] = {"cwd": cwd, "branch": branch, "count": 0, "last_ts": ts}
                s["locations"].append(loc_index[key])
            loc_index[key]["count"] += 1
            if ts > loc_index[key]["last_ts"]:
                loc_index[key]["last_ts"] = ts

        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if t == "user":
            if isinstance(content, list):
                for c in content:
                    # ツール結果の URL は gh pr create の結果だけを「作成した PR」として扱う
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pr_create_ids:
                        for m in PR_URL_RE.finditer(_tool_result_text(c)):
                            add_pr(m.group(0), True)
            if d.get("isMeta"):
                continue
            text = _user_text(content)
            if text is None:
                continue
            s["n_user"] += 1
            if len(s["first_prompts"]) < MAX_FIRST:
                s["first_prompts"].append(_clip(text))
            s["last_prompt"] = _clip(text)
            s["recent"].append({"role": "user", "text": _clip(text), "ts": ts})
            for m in PR_URL_RE.finditer(text):
                add_pr(m.group(0), False)
        elif t == "assistant" and isinstance(content, list):
            texts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    texts.append(c.get("text") or "")
                elif ct == "tool_use":
                    name = c.get("name")
                    inp = c.get("input") or {}
                    if name in _EDIT_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp and fp not in files_seen:
                            files_seen.add(fp)
                            s["files_edited"].append(fp)
                        ekey = (fp, d.get("gitBranch"))
                        if fp and ekey not in edits_seen and len(s["edits"]) < MAX_EDITS:
                            edits_seen.add(ekey)
                            s["edits"].append({"file": fp, "cwd": d.get("cwd"), "branch": d.get("gitBranch") or None})
                    elif name == "Bash":
                        cmd = inp.get("command") or ""
                        if _PR_CREATE_RE.search(cmd):
                            pr_create_ids.add(c.get("id"))
                        if _NOTABLE_CMD_RE.search(cmd) and len(s["commands"]) < MAX_COMMANDS:
                            s["commands"].append(_clip(cmd, 200))
            text = "\n".join(x for x in texts if x).strip()
            if text:
                s["n_assistant"] += 1
                s["recent"].append({"role": "assistant", "text": _clip(text), "ts": ts})
                for m in PR_URL_RE.finditer(text):
                    add_pr(m.group(0), False)
        if len(s["recent"]) > MAX_RECENT * 2:
            s["recent"] = s["recent"][-MAX_RECENT:]
    s["recent"] = s["recent"][-MAX_RECENT:]
    return s


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_lines(f)
