"""チケット (tickets.json) の形と操作。会話の ID の一覧を持つ入れ物。

設計は docs/session-and-ticket.md の 2 段目。1 つの会話は 1 つのチケットにだけ入る。
ここにあるのは data (tickets.json の中身) を変える純粋な関数だけで、ファイルの読み書きは store.JsonStore が行う。
"""

import re
import secrets

from .phase import PHASES

LIMITS = {"name": 200, "url": 2000, "project": 100, "memo": 4000, "target": 100}
DEFAULT_NAME = "新しいチケット"
_ID_RE = re.compile(r"^[0-9a-f]{10}$")
_SESSION_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")
_URL_RE = re.compile(r"^https?://[^\s]+$")


def empty():
    return {"tickets": []}


def ticket_key(ticket_id):
    return "tk:%s" % ticket_id


def valid_id(ticket_id):
    return isinstance(ticket_id, str) and bool(_ID_RE.match(ticket_id))


def valid_session_id(sid):
    return isinstance(sid, str) and bool(_SESSION_RE.match(sid))


def all_tickets(data):
    return [t for t in (data or {}).get("tickets") or [] if isinstance(t, dict) and valid_id(t.get("id"))]


def find(data, ticket_id):
    return next((t for t in all_tickets(data) if t["id"] == ticket_id), None)


def ticket_of_session(data):
    """会話の ID -> チケットの id。同じ会話が 2 つにあれば、先にあるチケットのもの。"""
    out = {}
    for t in all_tickets(data):
        for sid in t.get("session_ids") or []:
            out.setdefault(sid, t["id"])
    return out


def projects(data):
    """一度付けた案件の名前の一覧 (画面で選べるようにする)。"""
    return sorted({t.get("project") for t in all_tickets(data) if t.get("project")})


def _text(v, field):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("文字列で指定してください。")
    return v.strip()[:LIMITS[field]]


def clean_patch(patch):
    """チケットに書く値を確かめる。戻り値は書く値だけの dict。"""
    if not isinstance(patch, dict):
        raise ValueError("変更する内容の形が正しくありません。")
    out = {}
    if "name" in patch:
        name = _text(patch["name"], "name")
        if not name:
            raise ValueError("チケットの名前を入力してください。")
        out["name"] = name
    if "url" in patch:
        url = _text(patch["url"], "url")
        if url and not _URL_RE.match(url):
            raise ValueError("URL は http:// か https:// で始まる形で入力してください。")
        out["url"] = url
    if "project" in patch:
        out["project"] = _text(patch["project"], "project")
    if "target" in patch:
        # ボードの列。空なら中の会話から自動で決める。列の名前が正しいかはサーバーが確かめる
        out["target"] = _text(patch["target"], "target")
    if "memo" in patch:
        memo = patch["memo"]
        if memo is not None and not isinstance(memo, str):
            raise ValueError("文字列で指定してください。")
        out["memo"] = (memo or "")[:LIMITS["memo"]]
    if "phase" in patch:
        ph = patch["phase"] or ""
        if ph and ph not in PHASES:
            raise ValueError("「%s」は、フェーズに設定できません。" % str(ph)[:20])
        out["phase"] = ph
    if "hidden" in patch:
        out["hidden"] = bool(patch["hidden"])
    if "reopened" in patch:
        # 「進行中に戻す」: マージ済みの PR を除いてフェーズを決める
        out["reopened"] = bool(patch["reopened"])
    if not out:
        raise ValueError("変更する内容がありません。")
    return out


def _detach(data, sid):
    """会話を、入っているすべてのチケットから出す。"""
    for t in all_tickets(data):
        if sid in (t.get("session_ids") or []):
            t["session_ids"] = [x for x in t["session_ids"] if x != sid]


def create(data, name, session_ids, now, new_id=None, target=""):
    """チケットを作り、会話を入れる (ほかのチケットに入っていた会話は移す)。戻り値は新しいチケットの id。

    target はボードの列。空なら中の会話から自動で決める。
    """
    sids = list(dict.fromkeys(session_ids or []))
    if not all(valid_session_id(s) for s in sids):
        raise ValueError("会話の ID の形が正しくありません。")
    name = _text(name, "name") or DEFAULT_NAME
    existing = {t["id"] for t in all_tickets(data)}
    tid = new_id or secrets.token_hex(5)
    while tid in existing:
        tid = secrets.token_hex(5)
    for sid in sids:
        _detach(data, sid)
    data.setdefault("tickets", []).append({
        "id": tid, "name": name, "url": "", "project": "", "memo": "", "target": _text(target, "target"), "session_ids": sids,
        "created_at": now, "updated_at": now,
    })
    return tid


def add(data, ticket_id, session_id, now):
    t = find(data, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    if not valid_session_id(session_id):
        raise ValueError("会話の ID の形が正しくありません。")
    if session_id in (t.get("session_ids") or []):
        return False
    _detach(data, session_id)
    t.setdefault("session_ids", []).append(session_id)
    t["updated_at"] = now
    return True


def remove(data, ticket_id, session_id, now):
    t = find(data, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    if session_id not in (t.get("session_ids") or []):
        return False
    t["session_ids"] = [x for x in t["session_ids"] if x != session_id]
    t["updated_at"] = now
    return True


def update(data, ticket_id, patch, now):
    t = find(data, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    t.update(clean_patch(patch))
    t["updated_at"] = now


def merge(data, src_id, dst_id, now):
    """チケット src を dst に取り込む。src の会話を dst の後ろに足し、src を削除する。戻り値は移した会話の数。

    dst の名前・案件・列・フェーズはそのまま。src の URL とメモは捨てずに、dst のメモの後ろに書き足す
    (dst に URL が無ければ、src の URL を dst の URL にする)。
    """
    if src_id == dst_id:
        raise ValueError("同じチケットには取り込めません。")
    src, dst = find(data, src_id), find(data, dst_id)
    if not src or not dst:
        raise KeyError(src_id if not src else dst_id)
    moved = [sid for sid in src.get("session_ids") or [] if sid not in (dst.get("session_ids") or [])]
    dst["session_ids"] = list(dst.get("session_ids") or []) + moved
    url = src.get("url") or ""
    if url and not dst.get("url"):
        dst["url"] = url
        url = ""
    note = "\n".join(x for x in (url, (src.get("memo") or "").strip()) if x)
    if note:
        head = "「%s」から取り込み:" % (src.get("name") or DEFAULT_NAME)
        dst["memo"] = ((dst.get("memo") or "").rstrip() + "\n\n" + head + "\n" + note).strip()[:LIMITS["memo"]]
    dst["updated_at"] = now
    data["tickets"] = [x for x in data.get("tickets") or [] if x is not src]
    return len(moved)


def delete(data, ticket_id):
    """チケットを削除する。中の会話の ID の一覧を返す (会話の記録には触らない)。"""
    t = find(data, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    data["tickets"] = [x for x in data.get("tickets") or [] if x is not t]
    return list(t.get("session_ids") or [])
