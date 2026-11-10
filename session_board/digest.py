"""会話のまとめ。Claude Code の返事が終わるたびに (Stop のフック)、会話の記録から整理したデータ (JSON) と
ページ (HTML) を作り直す。

- 整理は別の claude -p に頼む。前の整理と、そのあとのやり取りを渡し、会話全体として整理し直させる
- 返ってきた文は yomiyasu の lint (yomiyasu_lint.py) で調べ、引っかかった項目だけを書き直させる。回数を決めて打ち切り、
  打ち切ったことはページに出す
- 置き場所はデータディレクトリの digests/ (<sessionId>.json と <sessionId>.html)

フックからは `python3 -m session_board.digest --hook` で呼ぶ。フックは裏の処理を起こしてすぐ戻る。
"""

import calendar
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from . import config
from .llm import WRITING_RULES, claude_env, resolve_claude_path
from .transcript import _EDIT_TOOLS, _user_text

DIGEST_DIR = "digests"
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

STEP_KINDS = ("依頼", "調べた", "決めた", "変えた", "確かめた", "その他")
SIDE_KINDS = ("あなたが聞いたこと", "あなたへの質問", "見つかった不具合", "あとでやる", "豆知識")
WHO = ("user", "claude")
CHANGES = ("same", "changed", "added", "removed")
LIMITS = {"theme": 80, "now": 80, "next": 80, "step_text": 60, "step_detail": 160, "why": 120,
          "side_title": 60, "side_detail": 200, "flow_name": 40, "node_text": 30, "node_sub": 40, "edge_label": 12}
MAX_STEPS = 12
MAX_SIDE = 20
MAX_FLOWS = 4
MAX_NODES = 16
MAX_MESSAGE_CHARS = 2000  # 1 つの発言として渡す長さ

SYSTEM_PROMPT = (
    "あなたは、開発者と Claude Code の会話を、読む人の負担が少ない形に整理するアシスタントです。"
    "出力は指定された形の JSON オブジェクト 1 つだけにしてください。前置きやコードフェンスは不要です。\n\n"
    + WRITING_RULES
)
SCHEMA = (
    '{"theme": "この会話で何の話をしているか (1 文、80 字以内)", '
    '"now": "いまの段で起きていること (1 文)", '
    '"next": {"text": "次にやること (1 文)", "who": "user|claude"} または null, '
    '"steps": [{"id": "s1 のような短い名前", "kind": "依頼|調べた|決めた|変えた|確かめた|その他", '
    '"text": "その段の一言 (60 字以内)", "detail": "補足 (160 字以内) か null", '
    '"why": "kind が決めた のときだけ、決めた理由 (120 字以内)。それ以外は null", '
    '"who": "user|claude|null", "state": "done|now"}], '
    '"side": [{"kind": "あなたが聞いたこと|あなたへの質問|見つかった不具合|あとでやる|豆知識", '
    '"title": "一言 (60 字以内)", "detail": "説明 (200 字以内) か null", '
    '"step": "出てきた段の id", "open": true|false}], '
    '"flows": [{"name": "処理の名前", '
    '"before": {"nodes": [{"id": "n1", "text": "箱の一言 (30 字以内)", "sub": "補足 (40 字以内) か null", '
    '"change": "same|removed"}], "edges": [{"from": "n1", "to": "n2", "label": "分かれ道の言葉 か null"}]}, '
    '"after": {"nodes": [{"id": "n1", "text": "...", "sub": "...", "change": "same|changed|added"}], "edges": [...]}}]}'
)
RULES = "\n".join([
    "整理のしかた:",
    "- 前の整理があっても、末尾に足していかない。会話全体として、いまの状態に合わせて並べ直す",
    "- theme: 会話の主題。途中で主題が変わったら、いまの主題を書く",
    "- steps: 主題についての話の進み方を古い順に、多くても 12 段。細かいやり取りはまとめる。"
    "決めたこと (kind=決めた) は必ず 1 段にし、why に理由を書く。いまの段は 1 つだけ state=now にする",
    "- side: 主題から外れた話。話の途中で利用者が聞いたこと、利用者が知らないことについて聞いたこと、"
    "Claude が利用者に聞いて答えを待っていること (kind=あなたへの質問)、見つかった別の不具合、あとでやると決めた作業、"
    "途中で調べた豆知識。step に、出てきた段の id を書く。片づいていなければ open=true",
    "- flows: プログラムの処理に手を入れた会話だけ。会話を始めたときの処理 (before) と、いまの処理 (after) を、"
    "箱と矢印で書く。手を入れた処理ごとに 1 組、多くても 4 組。箱は多くても 16 個。"
    "after で変えた箱は changed、足した箱は added。before でなくした箱は removed。コードの差分そのものは書かない。"
    "処理に手を入れていなければ空の配列",
    "- 「この会話で編集したファイル」があれば、処理に手を入れた会話として、flows を必ず 1 組以上作る。"
    "会話の文だけで処理の流れが分からなければ、必要に応じて、編集したファイルや関係するファイルを Read / Grep / Glob で読む。"
    "会話を始めたときの形は git diff / git log / git show で確かめてよい。ファイルを書き換えたり、ほかのコマンドを動かしたりはしない",
    "- 前の整理に flows があれば、理由がなければ消さずに引き継ぎ、そのあとの変更に合わせて直す",
    "- 主語を省かない。誰がしたことか (利用者か Claude か) が分かるように書く",
])


# ---------------------------------------------------------------- 置き場所

def digest_dir(data_dir=None):
    return os.path.join(data_dir or config.DATA_DIR, DIGEST_DIR)


def paths(session_id, data_dir=None):
    """(JSON, HTML) のパス。session_id の形がおかしければ ValueError。"""
    if not _SID_RE.match(session_id or ""):
        raise ValueError("bad session id")
    d = digest_dir(data_dir)
    return os.path.join(d, session_id + ".json"), os.path.join(d, session_id + ".html")


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load(session_id, data_dir=None):
    try:
        with open(paths(session_id, data_dir)[0], encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def brief(d):
    """詳細パネルに出す分だけを取り出す。主題、いまと次、段の一言、片づいていない脇の話、処理の図があるか。"""
    side = d.get("side") or []
    per_step = {}
    for sd in side:
        if sd.get("step"):
            per_step[sd["step"]] = per_step.get(sd["step"], 0) + 1
    return {
        "theme": d.get("theme"), "now": d.get("now"), "next": d.get("next"),
        "steps": [{"kind": st["kind"], "text": st["text"], "state": st["state"], "side": per_step.get(st["id"], 0)}
                  for st in d.get("steps") or []],
        "open": [{"kind": sd["kind"], "title": sd["title"]} for sd in side if sd.get("open")],
        "flows": len(d.get("flows") or []),
        "check": (d.get("check") or {}).get("status"),
        "updated_at": d.get("updated_at"),
    }


class BriefCache:
    """会話ごとの brief を、ファイルの更新時刻が変わったときだけ読み直す。"""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        self.cache = {}

    def get(self, session_id):
        try:
            jp = paths(session_id, self.data_dir)[0]
            mtime = os.path.getmtime(jp)
        except (ValueError, OSError):
            return None
        hit = self.cache.get(session_id)
        if hit and hit[0] == mtime:
            return hit[1]
        d = load(session_id, self.data_dir)
        b = brief(d) if d and d.get("theme") else None
        self.cache[session_id] = (mtime, b)
        return b


# ---------------------------------------------------------------- 会話の記録

def read_messages(lines):
    """会話の記録 (jsonl) から、利用者と Claude の発言を古い順に返す。[{role, text, ts}]。

    ツールの結果やサブエージェントの記録は入れない。ファイルの編集は「(ファイルを編集: 名前)」と短く残す。
    """
    out = []
    for raw in lines:
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        t = d.get("type")
        content = msg.get("content")
        if t == "user":
            text = _user_text(content)
            if text:
                out.append({"role": "user", "text": text, "ts": d.get("timestamp")})
        elif t == "assistant" and isinstance(content, list):
            parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and (c.get("text") or "").strip():
                    parts.append(c["text"].strip())
                elif c.get("type") == "tool_use" and c.get("name") in _EDIT_TOOLS:
                    fp = (c.get("input") or {}).get("file_path") or ""
                    if fp:
                        parts.append("(ファイルを編集: %s)" % os.path.basename(fp))
            if parts:
                out.append({"role": "assistant", "text": "\n".join(parts), "ts": d.get("timestamp")})
    return out


def _clip(text, n):
    text = (text or "").replace("\r", "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _render_messages(msgs, budget):
    """発言を文にする。budget を超えるときは、最初の 1/4 と最後の 3/4 を残して間を省く。"""
    lines = ["[%s] %s" % ("利用者" if m["role"] == "user" else "Claude", _clip(m["text"], MAX_MESSAGE_CHARS)) for m in msgs]
    if sum(len(x) + 1 for x in lines) <= budget:
        return "\n".join(lines)
    head, tail, used = [], [], 0
    for x in lines:
        if used + len(x) > budget // 4:
            break
        head.append(x)
        used += len(x) + 1
    used = 0
    for x in reversed(lines[len(head):]):
        if used + len(x) > budget - budget // 4:
            break
        tail.insert(0, x)
        used += len(x) + 1
    skipped = len(lines) - len(head) - len(tail)
    return "\n".join(head + ["(途中の %d 件の発言は長いので省いた)" % skipped] + tail)


def edited_files(lines, limit=40):
    """会話の中で編集したファイルの絶対パス (古い順、重なりなし)。処理の図を描くときに読ませるため。"""
    out = []
    for raw in lines:
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("isSidechain") or d.get("type") != "assistant":
            continue
        for c in (d.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in _EDIT_TOOLS:
                fp = (c.get("input") or {}).get("file_path") or (c.get("input") or {}).get("notebook_path")
                if fp and fp not in out:
                    out.append(fp)
    return out[-limit:]


def build_prompt(msgs, prev=None, budget=30000, files=None):
    """整理を頼む文。prev (前の整理) があれば、それと、そのあとの発言だけを渡す。files は編集したファイル。"""
    lines = ["次の Claude Code の会話を整理してください。", ""]
    seen = prev.get("seen", 0) if isinstance(prev, dict) else 0
    if prev and 0 < seen <= len(msgs):
        shown = {k: prev.get(k) for k in ("theme", "now", "next", "steps", "side", "flows")}
        lines.append("## 前の整理 (発言 %d 件目までを整理したもの)" % seen)
        lines.append(json.dumps(shown, ensure_ascii=False))
        lines.append("")
        lines.append("## そのあとのやり取り")
        lines.append(_render_messages(msgs[seen:], budget))
    else:
        lines.append("## 会話 (古い順)")
        lines.append(_render_messages(msgs, budget))
    if files:
        lines.append("")
        lines.append("## この会話で編集したファイル")
        lines.extend("- " + f for f in files)
    lines.append("")
    lines.append(RULES)
    lines.append("")
    lines.append("出力形式 (JSON のみ): " + SCHEMA)
    return "\n".join(lines)


# ---------------------------------------------------------------- 返事の検証

def _extract_object(stdout):
    if not stdout or not stdout.strip():
        return None
    text = stdout
    try:
        env = json.loads(stdout)
        if isinstance(env, dict) and "result" in env:
            if env.get("is_error"):
                return None
            text = env.get("result") or ""
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _s(v, n):
    if not isinstance(v, str):
        return None
    t = _clip(v, n)
    return t if t and t.lower() not in ("null", "none", "なし") else None


def _enum(v, allowed):
    return v.strip() if isinstance(v, str) and v.strip() in allowed else None


def _graph(v, changes):
    if not isinstance(v, dict):
        return {"nodes": [], "edges": []}
    nodes, ids = [], set()
    for n in v.get("nodes") if isinstance(v.get("nodes"), list) else []:
        if not isinstance(n, dict) or len(nodes) >= MAX_NODES:
            continue
        nid, text = _s(n.get("id"), 20), _s(n.get("text"), LIMITS["node_text"])
        if not nid or not text or nid in ids:
            continue
        ids.add(nid)
        nodes.append({"id": nid, "text": text, "sub": _s(n.get("sub"), LIMITS["node_sub"]),
                      "change": _enum(n.get("change"), changes) or "same"})
    edges = []
    for e in v.get("edges") if isinstance(v.get("edges"), list) else []:
        if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids and e.get("from") != e.get("to"):
            edges.append({"from": e["from"], "to": e["to"], "label": _s(e.get("label"), LIMITS["edge_label"])})
    return {"nodes": nodes, "edges": edges}


def validate(obj):
    """返ってきた整理を検証して、決まった形にそろえる。主題が無ければ None。"""
    if not isinstance(obj, dict):
        return None
    theme = _s(obj.get("theme"), LIMITS["theme"])
    if not theme:
        return None
    nx = obj.get("next")
    nxt = None
    if isinstance(nx, dict) and _s(nx.get("text"), LIMITS["next"]):
        nxt = {"text": _s(nx.get("text"), LIMITS["next"]), "who": _enum(nx.get("who"), WHO)}
    steps, sids = [], set()
    for st in obj.get("steps") if isinstance(obj.get("steps"), list) else []:
        if not isinstance(st, dict):
            continue
        text = _s(st.get("text"), LIMITS["step_text"])
        if not text:
            continue
        sid = _s(st.get("id"), 20) or "s%d" % (len(steps) + 1)
        while sid in sids:
            sid += "x"
        sids.add(sid)
        kind = _enum(st.get("kind"), STEP_KINDS) or "その他"
        steps.append({"id": sid, "kind": kind, "text": text, "detail": _s(st.get("detail"), LIMITS["step_detail"]),
                      "why": _s(st.get("why"), LIMITS["why"]) if kind == "決めた" else None,
                      "who": _enum(st.get("who"), WHO), "state": "now" if st.get("state") == "now" else "done"})
    steps = steps[-MAX_STEPS:]
    nows = [i for i, st in enumerate(steps) if st["state"] == "now"]
    for i in nows[:-1]:
        steps[i]["state"] = "done"
    if steps and not nows:
        steps[-1]["state"] = "now"
    ids = {st["id"] for st in steps}
    side = []
    for sd in obj.get("side") if isinstance(obj.get("side"), list) else []:
        if not isinstance(sd, dict) or len(side) >= MAX_SIDE:
            continue
        title, kind = _s(sd.get("title"), LIMITS["side_title"]), _enum(sd.get("kind"), SIDE_KINDS)
        if not title or not kind:
            continue
        side.append({"kind": kind, "title": title, "detail": _s(sd.get("detail"), LIMITS["side_detail"]),
                     "step": sd.get("step") if sd.get("step") in ids else None, "open": sd.get("open") is True})
    flows = []
    for fl in obj.get("flows") if isinstance(obj.get("flows"), list) else []:
        if not isinstance(fl, dict) or len(flows) >= MAX_FLOWS:
            continue
        name = _s(fl.get("name"), LIMITS["flow_name"])
        after = _graph(fl.get("after"), ("same", "changed", "added"))
        if name and after["nodes"]:
            flows.append({"name": name, "before": _graph(fl.get("before"), ("same", "removed")), "after": after})
    return {"theme": theme, "now": _s(obj.get("now"), LIMITS["now"]), "next": nxt,
            "steps": steps, "side": side, "flows": flows}


# ---------------------------------------------------------------- yomiyasu の lint

def yomiyasu_lint_path(settings, home=None):
    """yomiyasu_lint.py の場所。設定 yomiyasu_dir、~/.claude/skills/yomiyasu、~/.agents/skills/yomiyasu の順に探す。"""
    home = home or os.path.expanduser("~")
    dirs = []
    if settings.get("yomiyasu_dir"):
        c = settings["yomiyasu_dir"]
        dirs.append(os.path.join(home, c[2:]) if c.startswith("~/") else c)
    dirs += [os.path.join(home, ".claude", "skills", "yomiyasu"), os.path.join(home, ".agents", "skills", "yomiyasu")]
    for d in dirs:
        p = os.path.join(d, "scripts", "yomiyasu_lint.py")
        if os.path.isfile(p):
            return p
    return None


def text_fields(d):
    """lint にかける文の一覧。[(項目の場所, 文)]。場所は書き直しを頼むときにそのまま使う。"""
    out = []

    def add(path, v):
        if isinstance(v, str) and v.strip():
            out.append((path, v))

    add("theme", d.get("theme"))
    add("now", d.get("now"))
    if d.get("next"):
        add("next.text", d["next"].get("text"))
    for i, st in enumerate(d.get("steps") or []):
        for k in ("text", "detail", "why"):
            add("steps[%d].%s" % (i, k), st.get(k))
    for i, sd in enumerate(d.get("side") or []):
        for k in ("title", "detail"):
            add("side[%d].%s" % (i, k), sd.get(k))
    for i, fl in enumerate(d.get("flows") or []):
        add("flows[%d].name" % i, fl.get("name"))
        for side_name in ("before", "after"):
            for j, n in enumerate(fl[side_name]["nodes"]):
                for k in ("text", "sub"):
                    add("flows[%d].%s.nodes[%d].%s" % (i, side_name, j, k), n.get(k))
    return out


def run_lint(lint_path, text, timeout=60):
    """yomiyasu_lint.py --json を 1 回動かして、warn と error の候補を返す。[{rule, line, message, snippet}]。"""
    proc = subprocess.run([sys.executable, lint_path, "--json", "-"], input=text, capture_output=True, text=True,
                          timeout=timeout)
    obj = json.loads(proc.stdout or "{}")
    return [f for f in obj.get("findings") or [] if f.get("severity") in ("warn", "error")]


def lint_digest(d, lint):
    """整理の文を 1 項目 1 段落にして lint にかけ、引っかかった項目ごとに候補をまとめる。{場所: [候補]}。

    lint は run_lint と同じ形の関数 (文 -> 候補の一覧)。
    """
    fields = text_fields(d)
    doc = "\n\n".join(t.replace("\n", " ") for _, t in fields) + "\n"
    found = {}
    for f in lint(doc):
        k = (int(f.get("line") or 0) - 1) // 2
        if 0 <= k < len(fields):
            found.setdefault(fields[k][0], []).append(f)
    return found


def build_fix_prompt(d, found):
    lines = ["次の整理の JSON の中に、日本語の書き方の決まりに合わない文が見つかりました。",
             "見つかった項目の文だけを、意味を変えずに直してください。ほかの項目は変えないでください。", ""]
    by_path = dict(text_fields(d))
    for path, fs in found.items():
        lines.append("- %s: %s" % (path, by_path.get(path, "")))
        for f in fs:
            lines.append("  - %s" % (f.get("message") or f.get("rule")))
    lines.append("")
    lines.append("## 整理の JSON")
    lines.append(json.dumps({k: d.get(k) for k in ("theme", "now", "next", "steps", "side", "flows")}, ensure_ascii=False))
    lines.append("")
    lines.append("直した JSON 全体を、同じ形で出してください。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 作る

def generate(msgs, prev, settings, ask, lint=None, now=None, files=None):
    """整理を作る。ask は (prompt) -> claude の出力、lint は lint_digest に渡す関数 (None なら調べない)。

    戻り値は保存する dict。作れなかったら None。
    """
    d = validate(_extract_object(ask(build_prompt(msgs, prev, settings.get("digest_input_chars", 30000), files))))
    if d is None:
        return None
    rounds = int(settings.get("digest_lint_rounds", 2))
    if lint is None:
        d["check"] = {"status": "not_run", "rounds": 0, "left": []}
    else:
        found = lint_digest(d, lint)
        n = 0
        while found and n < rounds:
            n += 1
            fixed = validate(_extract_object(ask(build_fix_prompt(d, found))))
            if fixed is None:
                break
            d = fixed
            found = lint_digest(d, lint)
        left = [{"field": k, "rules": sorted({f.get("rule") or "" for f in v})} for k, v in found.items()]
        d["check"] = {"status": "left" if left else "ok", "rounds": n, "left": left}
    d["seen"] = len(msgs)
    d["updated_at"] = now if now is not None else time.time()
    return d


# ---------------------------------------------------------------- 処理の図 (SVG)

NODE_W = 260
ROW_GAP = 46
COL_GAP = 16
CHAR_W = 13  # 1 字の幅のめやす
LINE_H = 17


def _wrap(text, width_px, size=CHAR_W):
    per = max(4, int((width_px - 20) // size))
    return [text[i:i + per] for i in range(0, len(text), per)] or [""]


def layout(graph):
    """箱を段に並べる。段は、どの箱から何本たどって着くかのいちばん長い本数。戻る矢印は段を決めるのに使わない。

    戻り値: {"w", "h", "nodes": {id: {x, y, w, h, lines, sub_lines}}}
    """
    order = [n["id"] for n in graph["nodes"]]
    pos = {nid: i for i, nid in enumerate(order)}
    layer = {nid: 0 for nid in order}
    fwd = [e for e in graph["edges"] if pos[e["from"]] < pos[e["to"]]]
    for _ in range(len(order)):
        changed = False
        for e in fwd:
            if layer[e["to"]] < layer[e["from"]] + 1:
                layer[e["to"]] = layer[e["from"]] + 1
                changed = True
        if not changed:
            break
    rows = {}
    for nid in order:
        rows.setdefault(layer[nid], []).append(nid)
    widest = max(len(r) for r in rows.values()) if rows else 1
    w_total = max(NODE_W + 40, widest * 150 + (widest - 1) * COL_GAP + 40)
    by_id = {n["id"]: n for n in graph["nodes"]}
    boxes, y = {}, 12
    for li in sorted(rows):
        ids = rows[li]
        k = len(ids)
        w = NODE_W if k == 1 else min(NODE_W, (w_total - 40 - (k - 1) * COL_GAP) // k)
        start = (w_total - (k * w + (k - 1) * COL_GAP)) / 2
        row_h = 0
        for j, nid in enumerate(ids):
            n = by_id[nid]
            lines = _wrap(n["text"], w)
            subs = _wrap(n["sub"], w, 11) if n.get("sub") else []
            h = 16 + LINE_H * len(lines) + 15 * len(subs)
            boxes[nid] = {"x": start + j * (w + COL_GAP), "y": y, "w": w, "h": h, "lines": lines, "subs": subs}
            row_h = max(row_h, h)
        for nid in ids:
            boxes[nid]["row_h"] = row_h
        y += row_h + ROW_GAP
    return {"w": w_total, "h": y - ROW_GAP + 4, "nodes": boxes}


_TAGS = {"changed": "変えた", "added": "足した", "removed": "なくした"}


def render_svg(graph, label, marker_id):
    if not graph["nodes"]:
        return '<p class="empty">この処理は、会話を始めたときには無かった。</p>'
    L = layout(graph)
    e = html.escape
    # 幅と高さを書いて、左右の図を同じ倍率で描く (狭い画面では CSS の max-width で縮む)
    out = ['<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="%s">'
           % (L["w"], L["h"], L["w"], L["h"], e(label)),
           '<defs><marker id="%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path class="arrow" d="M0 0L10 5L0 10z"/></marker></defs>' % marker_id]
    B = L["nodes"]
    for ed in graph["edges"]:
        a, b = B[ed["from"]], B[ed["to"]]
        x1, y1 = a["x"] + a["w"] / 2, a["y"] + a["h"]
        x2, y2 = b["x"] + b["w"] / 2, b["y"]
        if y2 > y1:
            out.append('<path class="e" d="M%.0f %.0f L%.0f %.0f" marker-end="url(#%s)"/>' % (x1, y1, x2, y2 - 2, marker_id))
            # 言葉は矢印の出口の近くに、矢印の向きの外側へ置く
            lx, ly = x1 + (x2 - x1) * 0.3 + (6 if x2 >= x1 else -6), y1 + (y2 - y1) * 0.3 + 4
        else:
            # 前の箱へ戻る矢印は、右の外側を回す
            rx = L["w"] - 8
            ya, yb = a["y"] + a["h"] / 2, b["y"] + b["h"] / 2
            out.append('<path class="e" d="M%.0f %.0f H%.0f V%.0f H%.0f" marker-end="url(#%s)"/>'
                       % (a["x"] + a["w"], ya, rx, yb, b["x"] + b["w"] + 2, marker_id))
            lx, ly = rx - 4, (ya + yb) / 2
        if ed.get("label"):
            anchor = "end" if (y2 <= y1 or x2 < x1) else "start"
            out.append('<text class="lab" x="%.0f" y="%.0f" text-anchor="%s">%s</text>' % (lx, ly, anchor, e(ed["label"])))
    for n in graph["nodes"]:
        b = B[n["id"]]
        cls = {"changed": "n n--chg", "added": "n n--add", "removed": "n n--rm"}.get(n["change"], "n")
        out.append('<rect class="%s" x="%.0f" y="%.0f" width="%d" height="%d" rx="6"/>' % (cls, b["x"], b["y"], b["w"], b["h"]))
        cx = b["x"] + b["w"] / 2
        ty = b["y"] + 8 + LINE_H - 4
        bold = " t--b" if n["change"] != "same" else ""
        for line in b["lines"]:
            out.append('<text class="t%s" x="%.0f" y="%.0f" text-anchor="middle">%s</text>' % (bold, cx, ty, e(line)))
            ty += LINE_H
        for line in b["subs"]:
            out.append('<text class="t t--sub" x="%.0f" y="%.0f" text-anchor="middle">%s</text>' % (cx, ty - 2, e(line)))
            ty += 15
        tag = _TAGS.get(n["change"])
        if tag:
            tx = b["x"] + b["w"] - 50
            out.append('<rect class="tag" x="%.0f" y="%.0f" width="46" height="17" rx="8"/>' % (tx, b["y"] - 9))
            out.append('<text class="tag-t" x="%.0f" y="%.0f" text-anchor="middle">%s</text>' % (tx + 23, b["y"] + 3, tag))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- ページ (HTML)

PAGE_CSS = """
:root{color-scheme:light;--bg:#f4f4f1;--surface:#fff;--surface-2:#f0f0ec;--surface-sunk:#e9e9e4;--line:#e1e1db;--line-strong:#c9c9c1;
--text:#1a1b1d;--text-2:#45474b;--text-3:#696c71;--mine:#4f46e5;--mine-ink:#fff;--mine-bg:#eeedfd;--mine-line:#b9b4f6;--mine-text:#3730a3;
--font-sans:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;--font-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
--r-1:4px;--r-2:6px;--r-3:8px;--r-pill:999px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111214;--surface:#1a1b1e;--surface-2:#212226;--surface-sunk:#16171a;
--line:#2b2d32;--line-strong:#41444b;--text:#e8e8e5;--text-2:#bcbec2;--text-3:#909399;--mine:#8b85ff;--mine-ink:#12102b;--mine-bg:#1f1d3d;
--mine-line:#4a4494;--mine-text:#c2bfff}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111214;--surface:#1a1b1e;--surface-2:#212226;--surface-sunk:#16171a;
--line:#2b2d32;--line-strong:#41444b;--text:#e8e8e5;--text-2:#bcbec2;--text-3:#909399;--mine:#8b85ff;--mine-ink:#12102b;--mine-bg:#1f1d3d;
--mine-line:#4a4494;--mine-text:#c2bfff}
*{box-sizing:border-box}
.bar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:10px max(16px,env(safe-area-inset-right,0px)) 10px max(16px,env(safe-area-inset-left,0px));background:var(--surface-2);border-bottom:1px solid var(--line)}
.bar__l{display:grid;gap:2px;min-width:0;margin-right:auto}
.brand{font-size:15px;font-weight:700;color:var(--text);text-decoration:none;justify-self:start}
.crumbs ol{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:2px 6px;font-size:12px;color:var(--text-3)}
.crumbs li{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:40em}
.crumbs li+li::before{content:"›";margin-right:6px;color:var(--line-strong)}
.crumbs a{color:var(--text-2)}
.crumbs [aria-current]{color:var(--text-2);font-weight:600}
.themebtn{height:30px;padding:0 12px;border:1px solid var(--line-strong);border-radius:var(--r-2);background:var(--surface);color:var(--text);font:inherit;font-size:12px;font-weight:600;cursor:pointer}
.themebtn[hidden]{display:none}
:focus-visible{outline:2px solid var(--text);outline-offset:2px}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-sans);font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1160px;margin:0 auto;padding-block:28px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:36px}
.eyebrow{font-size:11px;letter-spacing:.08em;color:var(--text-3);font-weight:600}
.head{display:grid;gap:10px}
.head h1{margin:0;font-size:24px;line-height:1.4;text-wrap:balance;max-width:34em}
.facts{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px;color:var(--text-3)}
.facts b{color:var(--text-2);font-weight:600}
.sec{display:grid;gap:14px}
.sec h2{margin:0;font-size:15px;display:flex;flex-wrap:wrap;align-items:baseline;gap:10px}
.sec h2 small{font-weight:400;font-size:12px;color:var(--text-3)}
.flow{display:grid;grid-template-columns:76px minmax(0,1fr) minmax(0,340px)}
.colhead{font-size:11px;color:var(--text-3);letter-spacing:.06em;font-weight:600;padding-bottom:8px}
.colhead--side{padding-left:40px}
.step{display:contents}
.kind{padding-top:13px;font-size:12px;color:var(--text-3);font-weight:600;text-align:right;padding-right:14px}
.main{position:relative;padding:0 0 18px 26px}
.main::before{content:"";position:absolute;left:6px;top:0;bottom:0;width:2px;background:var(--line-strong)}
.step--first .main::before{top:18px}
.step--last .main::before{bottom:auto;height:18px}
.step--first.step--last .main::before{display:none}
.step--next .main::before{background:repeating-linear-gradient(var(--line-strong) 0 4px,transparent 4px 8px)}
.dot{position:absolute;left:0;top:12px;width:14px;height:14px;border-radius:50%;background:var(--text-3);border:2px solid var(--text-3)}
.step--now .dot{background:var(--text);border-color:var(--text);box-shadow:0 0 0 4px var(--surface-sunk)}
.step--next .dot{background:var(--surface);border:2px dashed var(--line-strong)}
.step--mine .dot{border-color:var(--mine)}
.card{border:1px solid transparent;border-radius:var(--r-3);padding:10px 0;display:grid;gap:4px}
.card__t{font-weight:500}
.card__d{color:var(--text-2);font-size:12px}
.card--dec{background:var(--surface);border:2px solid var(--text);padding:10px 14px}
.card--dec .card__t{font-weight:700}
.card--now{background:var(--surface);border:2px solid var(--text);padding:10px 14px}
.card--now .card__t{font-weight:600}
.card--next{background:var(--surface);border:1px dashed var(--line-strong);padding:10px 14px}
.card--mine{background:var(--mine-bg);border:1px dashed var(--mine)}
.card--mine .card__d{color:var(--mine-text)}
.pill{justify-self:start;font-size:11px;font-weight:700;padding:1px 8px;border-radius:var(--r-pill);letter-spacing:.04em}
.pill--dec{background:var(--text);color:var(--bg)}
.pill--here{border:1px solid var(--text)}
.pill--mine{background:var(--mine);color:var(--mine-ink)}
.why{font-size:12px;color:var(--text-2)}
.why b{color:var(--text-3)}
.side{position:relative;padding:0 0 18px 40px;display:grid;gap:8px;align-content:start}
.note{position:relative;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-2);padding:8px 12px;display:grid;gap:2px;font-size:12px}
.note::before{content:"";position:absolute;left:-32px;top:16px;width:30px;border-top:1px dashed var(--line-strong)}
.note+.note::before{display:none}
.note__k{font-size:11px;font-weight:700;color:var(--text-3);letter-spacing:.04em}
.note__t{color:var(--text);font-weight:600;font-size:14px}
.note__d{color:var(--text-2)}
.note--mine{background:var(--mine-bg);border-color:var(--mine-line)}
.note--mine .note__k{color:var(--mine-text)}
.state{justify-self:start;margin-top:4px;font-size:11px;padding:0 7px;border-radius:var(--r-pill);border:1px solid var(--line-strong);color:var(--text-3)}
.note--mine .state{border-color:var(--mine);color:var(--mine-text)}
.loose{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.loose .note::before{display:none}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px;color:var(--text-3)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:18px;height:12px;border-radius:3px;border:1px solid var(--line-strong);background:var(--surface)}
.sw--chg{border:2.5px solid var(--text)}.sw--add{border:2.5px dashed var(--text)}.sw--rm{border:1px dashed var(--line-strong);background:var(--surface-sunk)}
.ba{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.ba__col{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-3);padding:14px;display:grid;gap:10px;align-content:start}
.ba__col h3{margin:0;font-size:12px;color:var(--text-3);letter-spacing:.06em}
.ba__scroll{overflow-x:auto}
.ba svg{display:block;max-width:100%;height:auto;margin:0 auto}
.empty{color:var(--text-3);font-size:12px;margin:0}
svg .n{fill:var(--surface);stroke:var(--line-strong);stroke-width:1.2}
svg .n--chg{stroke:var(--text);stroke-width:2.5}
svg .n--add{stroke:var(--text);stroke-width:2.5;stroke-dasharray:6 4}
svg .n--rm{fill:var(--surface-sunk);stroke:var(--line-strong);stroke-dasharray:4 3}
svg .t{fill:var(--text);font-size:13px;font-family:var(--font-sans)}
svg .t--sub{fill:var(--text-2);font-size:11px}
svg .t--b{font-weight:700}
svg .e{stroke:var(--text-3);stroke-width:1.4;fill:none}
svg .lab{fill:var(--text-3);font-size:11px;font-family:var(--font-sans)}
svg .tag{fill:var(--text)}
svg .tag-t{fill:var(--bg);font-size:10px;font-weight:700;font-family:var(--font-sans)}
svg .arrow{fill:var(--text-3)}
.foot{font-size:12px;color:var(--text-3);border-top:1px solid var(--line);padding-top:12px}
.foot--warn{color:var(--text-2)}
code{font-family:var(--font-mono);font-size:.9em;background:var(--surface-sunk);padding:1px 5px;border-radius:var(--r-1)}
@media (max-width:860px){.flow{grid-template-columns:56px minmax(0,1fr)}.colhead--side{display:none}
.side{grid-column:2;padding:0 0 18px 26px}.side:empty{display:none}.note::before{display:none}.ba{grid-template-columns:minmax(0,1fr)}}
"""

_WHO_JA = {"user": "あなた", "claude": "Claude"}


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        if not isinstance(ts, (int, float)):
            # 会話の記録の時刻は UTC の ISO 形式
            ts = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%m/%d %H:%M", time.localtime(ts))
    except (ValueError, OverflowError):
        return ""


def _note(sd):
    e = html.escape
    mine = sd["kind"] == "あなたへの質問" and sd["open"]
    if sd["kind"] == "あなたへの質問":
        state = "答えを待っている" if sd["open"] else "答えた"
    elif sd["kind"] in ("見つかった不具合", "あとでやる"):
        state = "まだ片づいていない" if sd["open"] else "片づいた"
    else:
        state = None
    parts = ['<div class="note%s">' % (" note--mine" if mine else ""),
             '<span class="note__k">%s</span>' % e(sd["kind"]),
             '<span class="note__t">%s</span>' % e(sd["title"])]
    if sd.get("detail"):
        parts.append('<span class="note__d">%s</span>' % e(sd["detail"]))
    if state:
        parts.append('<span class="state">%s</span>' % state)
    parts.append("</div>")
    return "".join(parts)


def _bar(crumbs):
    """一番上の帯: ロゴ、その下にパンくずリスト、右にライトとダークの切り替え。

    crumbs は [列の名前, (チケットの名前,) 会話のタイトル] (サーバーが返すとき)。最後が今のページ。
    無ければ「会話のまとめ」だけを出す。切り替えのボタンは /static/digest.js が出す (ファイルを直接開いたときは出ない)。
    """
    e = html.escape
    items = ['<li><a href="/">ボード</a></li>']
    if crumbs:
        for c in crumbs[:-1]:
            if c:
                items.append("<li>%s</li>" % e(c))
        items.append('<li aria-current="page">%s</li>' % e(crumbs[-1] or "会話のまとめ"))
    else:
        items.append('<li aria-current="page">会話のまとめ</li>')
    return ('<header class="bar"><div class="bar__l"><a class="brand" href="/">session-board</a>'
            '<nav class="crumbs" aria-label="現在地"><ol>%s</ol></nav></div>'
            '<button type="button" class="themebtn" id="theme-toggle" hidden>ライト / ダークを切り替え</button></header>'
            % "".join(items))


def render_html(d, meta=None, crumbs=None):
    """整理を 1 枚のページにする。meta は {"cwd", "first_ts", "last_ts"} (どれも省いてよい)。crumbs は _bar を見る。"""
    e = html.escape
    meta = meta or {}
    steps = d.get("steps") or []
    by_step = {}
    loose = []
    for sd in d.get("side") or []:
        (by_step.setdefault(sd["step"], []) if sd.get("step") else loose).append(sd)
    rows = []
    nxt = d.get("next")
    total = len(steps) + (1 if nxt else 0)
    for i, st in enumerate(steps):
        cls = ["step"]
        if i == 0:
            cls.append("step--first")
        if i == total - 1:
            cls.append("step--last")
        if st["state"] == "now":
            cls.append("step--now")
        if st["kind"] == "決めた":
            card = ['<div class="card card--dec"><span class="pill pill--dec">決めたこと</span>']
        elif st["state"] == "now":
            card = ['<div class="card card--now"><span class="pill pill--here">いまここ</span>']
        else:
            card = ['<div class="card">']
        card.append('<div class="card__t">%s</div>' % e(st["text"]))
        if st.get("why"):
            card.append('<div class="why"><b>理由</b> %s</div>' % e(st["why"]))
        if st.get("detail"):
            card.append('<div class="card__d">%s</div>' % e(st["detail"]))
        if st.get("who"):
            card.append('<div class="card__d">%sがした</div>' % _WHO_JA[st["who"]])
        card.append("</div>")
        kind = "いま" if st["state"] == "now" else st["kind"]
        rows.append('<div class="%s"><div class="kind">%s</div><div class="main"><span class="dot"></span>%s</div>'
                    '<div class="side">%s</div></div>'
                    % (" ".join(cls), e(kind), "".join(card), "".join(_note(sd) for sd in by_step.get(st["id"], []))))
    if nxt:
        mine = nxt.get("who") == "user"
        cls = "step step--next step--last" + (" step--mine" if mine else "") + (" step--first" if not steps else "")
        pill = '<span class="pill pill--mine">あなたの番</span>' if mine else ""
        who = '<div class="card__d">%sがやる</div>' % _WHO_JA[nxt["who"]] if nxt.get("who") else ""
        rows.append('<div class="%s"><div class="kind">次</div><div class="main"><span class="dot"></span>'
                    '<div class="card card--next%s">%s<div class="card__t">%s</div>%s</div></div><div class="side"></div></div>'
                    % (cls, " card--mine" if mine else "", pill, e(nxt["text"]), who))
    facts = []
    if meta.get("cwd"):
        facts.append('<span>場所 <b>%s</b></span>' % e(os.path.basename(meta["cwd"].rstrip("/")) or meta["cwd"]))
    if meta.get("first_ts"):
        facts.append('<span>始めた日時 <b>%s</b></span>' % e(_fmt_time(meta["first_ts"])))
    if d.get("updated_at"):
        facts.append('<span>整理した日時 <b>%s</b></span>' % e(_fmt_time(d["updated_at"])))
    if d.get("now"):
        facts.append('<span>いま <b>%s</b></span>' % e(d["now"]))
    body = ['<main class="wrap">',
            '<header class="head"><div class="eyebrow">この会話の主題</div><h1>%s</h1><div class="facts">%s</div></header>'
            % (e(d["theme"]), "".join(facts))]
    if rows:
        body.append('<section class="sec"><h2>話の進み方 <small>古い順。太い枠は決めたこと、黒い点がいまの段</small></h2>'
                    '<div class="flow"><div></div><div class="colhead">主題の流れ</div>'
                    '<div class="colhead colhead--side">脇に出した話</div>%s</div></section>' % "".join(rows))
    if loose:
        body.append('<section class="sec"><h2>脇に出した話 <small>どの段で出てきたかが分からないもの</small></h2>'
                    '<div class="loose">%s</div></section>' % "".join(_note(sd) for sd in loose))
    for i, fl in enumerate(d.get("flows") or []):
        body.append('<section class="sec"><h2>処理の流れ <small>%s。会話を始めたときと、いまを見比べる</small></h2>'
                    '<div class="legend"><span><i class="sw"></i>変えていない</span><span><i class="sw sw--chg"></i>変えた</span>'
                    '<span><i class="sw sw--add"></i>足した</span><span><i class="sw sw--rm"></i>なくした</span></div>'
                    '<div class="ba"><div class="ba__col"><h3>会話を始めたとき</h3><div class="ba__scroll">%s</div></div>'
                    '<div class="ba__col"><h3>いま</h3><div class="ba__scroll">%s</div></div></div></section>'
                    % (e(fl["name"]), render_svg(fl["before"], fl["name"] + " (会話を始めたとき)", "ab%d" % i),
                       render_svg(fl["after"], fl["name"] + " (いま)", "aa%d" % i)))
    chk = d.get("check") or {}
    if chk.get("status") == "ok":
        body.append('<p class="foot">文は yomiyasu の決まりで調べ、引っかかったところはありませんでした。</p>')
    elif chk.get("status") == "left":
        body.append('<p class="foot foot--warn">文を yomiyasu の決まりで調べ、%d 回書き直させましたが、%d か所が直りきりませんでした (%s)。</p>'
                    % (chk.get("rounds", 0), len(chk.get("left") or []),
                       e(", ".join(x["field"] for x in chk.get("left") or []))))
    else:
        body.append('<p class="foot foot--warn">yomiyasu が見つからなかったので、文を調べていません。</p>')
    body.append("</main>")
    # digest.js は head で読み、保存したライト / ダークを描く前に当てる
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<title>%s</title><style>%s</style><script src="/static/digest.js"></script></head><body>%s%s</body></html>'
            % (e(d["theme"]), PAGE_CSS, _bar(crumbs), "".join(body)))


# ---------------------------------------------------------------- 実行

# 整理する Claude に使わせる道具。読むものだけで、git は差分と履歴を見るコマンドだけを許す
DIGEST_TOOLS = "Read,Grep,Glob,Bash"
DIGEST_ALLOWED = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"


def run_digest_claude(prompt, settings, cwd, claude_path):
    """整理を頼む claude -p を 1 回動かして stdout を返す。会話の場所 (cwd) で動かし、必要ならファイルを読ませる。

    --setting-sources "" でフック (このフックを含む) を動かさず、--no-session-persistence で記録も残さない。
    許していない道具 (書き込みやほかのコマンド) は -p では断られる。
    """
    cmd = [
        claude_path, "-p",
        "--model", settings.get("digest_model") or settings.get("llm_model", "sonnet"),
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", DIGEST_TOOLS,
        "--allowedTools", DIGEST_ALLOWED,
        "--strict-mcp-config",
        "--setting-sources", "",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    os.makedirs(cwd, exist_ok=True)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=claude_env(),
                          timeout=int(settings.get("digest_timeout_seconds", 300)))
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError("claude exited %d: %s" % (proc.returncode, proc.stderr[-300:]))
    return proc.stdout


def run_once(session_id, transcript_path, settings, data_dir=None, ask=None, lint=None):
    """1 つの会話の整理を作り直して保存する。新しい発言が無ければ何もしない。戻り値は保存した dict か None。"""
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        raw = f.readlines()
    msgs = read_messages(raw)
    if not msgs:
        return None
    prev = load(session_id, data_dir)
    if prev and prev.get("seen") == len(msgs):
        return None
    ddir = digest_dir(data_dir)
    meta = {"first_ts": msgs[0].get("ts")}
    for line in raw:
        try:
            cwd = json.loads(line).get("cwd")
        except (ValueError, AttributeError):
            continue
        if cwd:
            meta["cwd"] = cwd
    files = edited_files(raw)
    if ask is None:
        claude_path = resolve_claude_path(settings.get("llm_claude_path"))
        workdir = meta["cwd"] if meta.get("cwd") and os.path.isdir(meta["cwd"]) else os.path.join(ddir, "work")

        def ask(prompt):
            return run_digest_claude(prompt, settings, workdir, claude_path)
    if lint is None:
        lp = yomiyasu_lint_path(settings)
        if lp:
            def lint(text):
                return run_lint(lp, text)
    d = generate(msgs, prev, settings, ask, lint, files=files)
    if d is None:
        return None
    d["meta"] = meta
    jp, hp = paths(session_id, data_dir)
    _write_atomic(jp, json.dumps(d, ensure_ascii=False, indent=1))
    _write_atomic(hp, render_html(d, meta))
    return d


def run_locked(session_id, transcript_path, settings, data_dir=None):
    """同じ会話の整理は 1 つずつ。動いている最中に頼まれたら、終わったあとにもう 1 回だけ作り直す。"""
    ddir = digest_dir(data_dir)
    os.makedirs(ddir, exist_ok=True)
    lock_path = os.path.join(ddir, session_id + ".lock")
    again = os.path.join(ddir, session_id + ".again")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            open(again, "w").close()
            return
        for _ in range(3):
            try:
                os.remove(again)
            except OSError:
                pass
            run_once(session_id, transcript_path, settings, data_dir)
            if not os.path.exists(again):
                break


def hook_main(stdin, settings, data_dir=None, spawn=None):
    """Stop のフックとして呼ばれたとき。会話の ID と記録の場所を読み、裏で整理を起こしてすぐ戻る。"""
    if not settings.get("digest_enabled", True):
        return
    try:
        ev = json.loads(stdin or "{}")
    except ValueError:
        return
    sid, tp = ev.get("session_id"), ev.get("transcript_path")
    if not isinstance(sid, str) or not _SID_RE.match(sid) or not isinstance(tp, str) or not os.path.isfile(tp):
        return
    ddir = digest_dir(data_dir)
    os.makedirs(ddir, exist_ok=True)
    cmd = [sys.executable, "-m", "session_board.digest", "--run", sid, tp]
    if spawn is not None:
        spawn(cmd)
        return
    with open(os.path.join(ddir, "digest.log"), "a") as log:
        subprocess.Popen(cmd, cwd=config.ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env=dict(os.environ, PYTHONPATH=config.ROOT))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        settings = config.load_settings()
    except config.ConfigError:
        settings = dict(config.DEFAULT_SETTINGS)
    if argv[:1] == ["--hook"]:
        try:
            hook_main(sys.stdin.read(), settings)
        except Exception:  # noqa: BLE001 - フックは Claude Code の作業を止めない
            pass
        return 0
    if argv[:1] == ["--run"] and len(argv) == 3:
        run_locked(argv[1], argv[2], settings)
        return 0
    print("使い方: python3 -m session_board.digest --hook | --run <sessionId> <会話の記録のパス>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
