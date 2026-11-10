"""「ゴミ箱へ」(旧名: 片付ける): 作業をボードに表示しないようにし、開いているタブを閉じ、Claude の会話の記録をゴミ箱へ移す。

- ブランチと worktree には触らない
- 消さない。ゴミ箱へ移すだけ (Finder の「戻す」が効くように、まず Finder に頼む。だめなら ~/.Trash へ rename)
- 動いている Claude のセッション (~/.claude/sessions/*.json の pid が生きている) の記録は移さない。
  同じ操作でそのタブを閉じる場合だけ、閉じた後に少し待って終了を確かめてから移す
- 対象は <Claude の設定>/projects/<ディレクトリ>/<sessionId>.jsonl と、同じ名前のディレクトリ (サブエージェントの
  記録・ツールの結果など) だけ。シンボリックリンクは辿らない
"""

import glob
import json
import os
import re
import time

SESSION_ID_RE = re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f-]{7,63}$")
LOG_NAME = "cleanup-log.jsonl"

# Finder にゴミ箱へ移させ、移動先のパスを返す AppleScript。パスは引数で渡す (文字列に埋め込まない)
_FINDER_SCRIPT = [
    "-e", "on run argv",
    "-e", 'tell application "Finder" to set t to delete (POSIX file (item 1 of argv) as alias)',
    "-e", "return POSIX path of (t as alias)",
    "-e", "end run",
]


def valid_session_id(sid):
    return isinstance(sid, str) and bool(SESSION_ID_RE.match(sid))


def safe_session_path(path, projects_dir, session_id):
    """path が projects_dir/<1 階層>/<session_id>(.jsonl) そのもので、リンクを含まないときだけ True。"""
    if not valid_session_id(session_id) or not isinstance(path, str):
        return False
    name = os.path.basename(path)
    if name not in (session_id, session_id + ".jsonl"):
        return False
    root = os.path.realpath(projects_dir)
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.islink(path) or os.path.islink(parent):
        return False
    real_parent = os.path.realpath(parent)
    if os.path.dirname(real_parent) != root or real_parent == root:
        return False
    if os.path.realpath(path) != os.path.join(real_parent, name):
        return False
    if name.endswith(".jsonl"):
        return os.path.isfile(path)
    return os.path.isdir(path)


def session_paths(projects_dir, session_id):
    """そのセッションの記録 (全プロジェクトの .jsonl と同名のディレクトリ) のうち安全なもの。"""
    if not valid_session_id(session_id) or not os.path.isdir(projects_dir):
        return []
    found = glob.glob(os.path.join(glob.escape(projects_dir), "*", glob.escape(session_id) + ".jsonl"))
    found += glob.glob(os.path.join(glob.escape(projects_dir), "*", glob.escape(session_id)))
    return sorted(p for p in set(found) if safe_session_path(p, projects_dir, session_id))


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_session_ids(sessions_dir, is_alive=pid_alive):
    """~/.claude/sessions/*.json のうち、pid が生きているものの {sessionId: pid}。"""
    out = {}
    for path in glob.glob(os.path.join(glob.escape(sessions_dir), "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(sid, str) and is_alive(pid):
            out[sid] = pid
    return out


def build_plan(item, paths_by_sid, live, can_close, shared, own_tty=None):
    """何をするかの一覧 (純粋関数)。

    paths_by_sid: {sessionId: [ゴミ箱へ移すパス]}、live: {sessionId: pid}、
    shared: {sessionId: [そのセッションを含むほかの案件のタイトル]}。
    own_tty: このボードのプロセスの tty。そのタブは閉じない (閉じるとボードが止まる)。
    セッションの action: trash (移す) / after_close (タブを閉じて終了を確かめてから移す) / skip (残す)。
    """
    tabs, kept = [], []
    for t in item.get("tabs") or [] if can_close else []:
        if not t.get("id"):
            continue
        x = {"id": t["id"], "title": t.get("title") or "", "group": t.get("group") or t.get("workspace_title") or "",
             "session_id": t.get("session_id")}
        if own_tty and t.get("tty") == own_tty:
            kept.append(dict(x, reason="このボードが動いているタブなので、閉じません"))
        else:
            tabs.append(x)
    closing = {t["session_id"] for t in tabs if t.get("session_id")}
    sessions = []
    for s in item.get("sessions") or []:
        sid = s.get("session_id")
        if not valid_session_id(sid):
            continue
        paths = list(paths_by_sid.get(sid) or [])
        is_live = sid in live
        action, reason = "trash", None
        if shared.get(sid):
            action = "skip"
            reason = "ほかの作業 (%s) にも含まれているため、残します" % "、".join(shared[sid][:3])
        elif not paths:
            action, reason = "skip", "会話の記録のファイルがないため、残します"
        elif is_live and sid in closing:
            action = "after_close"
        elif is_live:
            action = "skip"
            reason = "Claude が動いているため、残します。タブを閉じるか Claude を終了してから、やり直してください"
        sessions.append({"session_id": sid, "paths": paths, "live": is_live, "action": action, "reason": reason})
    untouched = []
    for b in item.get("branches") or []:
        label = "%s:%s" % (b.get("repo"), b.get("branch"))
        if b.get("worktree"):
            label += " (worktree %s)" % b["worktree"]
        untouched.append(label)
    moving = [s for s in sessions if s["action"] != "skip"]
    return {
        "item_id": item.get("id"),
        "title": item.get("title"),
        "hide": True,
        "already_hidden": bool(item.get("hidden")),
        "can_close": bool(can_close),
        "tabs": tabs,
        "kept_tabs": kept,
        "sessions": sessions,
        "untouched": untouched,
        "counts": {"tabs": len(tabs), "trash": len(moving), "skip": len(sessions) - len(moving),
                   "files": sum(len(s["paths"]) for s in moving)},
    }


def unique_trash_path(trash_dir, name, now, exists=os.path.lexists):
    """ゴミ箱の中で重ならない名前。重なるときは「名前 YYYY-MM-DD HH.MM.SS[ N].拡張子」。"""
    cand = os.path.join(trash_dir, name)
    if not exists(cand):
        return cand
    stem, ext = (name[:-6], ".jsonl") if name.endswith(".jsonl") else (name, "")
    stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime(now))
    for i in range(1, 1000):
        suffix = "" if i == 1 else " %d" % i
        cand = os.path.join(trash_dir, "%s %s%s%s" % (stem, stamp, suffix, ext))
        if not exists(cand):
            return cand
    raise OSError("ゴミ箱の中に空いている名前がありません: " + name)


def move_to_trash(path, trash_dir, runner, now=None):
    """path をゴミ箱へ移す。戻り値 {original, trash_path, method}。移せなければ OSError (消すことはしない)。"""
    rc, out, _ = runner(["osascript"] + _FINDER_SCRIPT + [path], timeout=30)
    dest = (out or "").strip()
    if rc == 0 and dest and not os.path.lexists(path):
        return {"original": path, "trash_path": dest.rstrip("/") if len(dest) > 1 else dest, "method": "finder"}
    if not os.path.lexists(path):
        # Finder は移したが移動先が分からない
        return {"original": path, "trash_path": None, "method": "finder"}
    os.makedirs(trash_dir, exist_ok=True)
    dest = unique_trash_path(trash_dir, os.path.basename(path), now if now is not None else time.time())
    os.rename(path, dest)  # 同じボリュームの中だけ。別のボリュームなら OSError (コピーして消すことはしない)
    return {"original": path, "trash_path": dest, "method": "rename"}


def own_tty(runner):
    """このプロセスの tty ('ttys003' など)。無ければ None。"""
    rc, out, _ = runner(["ps", "-o", "tty=", "-p", str(os.getpid())], timeout=5)
    tty = (out or "").strip() if rc == 0 else ""
    return tty if tty and tty not in ("??", "-") else None


def append_log(data_dir, record):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, LOG_NAME)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
