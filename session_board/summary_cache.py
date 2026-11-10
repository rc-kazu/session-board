"""要約キャッシュ (data/llm_cache.json) の形式・移行・取り直しの判定。純粋関数が中心。

形式 (version 2):
  sessions:      {sessionId: {summary, summarized_at, n_user, last_ts, mtime}}
  items:         {itemId: {summary, summarized_at, members, member_stamps}}
  legacy_items:  {旧ハッシュ: summary}  旧形式の案件要約。一致する案件が見つかれば items に移す
  failures:      {"s:<sessionId>" | "i:<itemId>": 失敗時刻}

要約は常に最新のものを表示し (stale-while-revalidate)、意味のある変化があったときだけ取り直す。
"""

import hashlib
import json
import os
import shutil

from .phase import parse_iso

VERSION = 2
DEFAULT_MIN_NEW_USER_MSGS = 3
DEFAULT_MIN_INTERVAL_MINUTES = 60
# 画面の「次に動く人」「いま / 次」「流れ図」用に後から足した項目 (docs/ui-spec.md 6 章)
NEW_FIELDS = ("ball", "ball_reason", "now_short", "next_short", "flow_steps")
# チケットのまとめにだけある項目 (どういう話か・今どうなっているか・会話どうしのつながり)
TICKET_FIELDS = ("background", "state_now", "links")
SUMMARY_FIELDS = ("title", "goal", "done_so_far", "next_action", "phase_guess", "blocked_question") + NEW_FIELDS + TICKET_FIELDS


def empty():
    return {"version": VERSION, "sessions": {}, "items": {}, "legacy_items": {}, "failures": {}}


def clean_summary(d):
    return {k: d.get(k) for k in SUMMARY_FIELDS if k in d}


def lacks_new_fields(summary):
    """NEW_FIELDS が入る前に作った要約か (値が null なのは「入っている」扱い)。

    流れ図の各段に detail (押すと出す詳しい説明) が入る前に作った要約も含める。
    今の版で作った要約は、detail が null でもキーはあるので、何度も取り直すことはない。
    """
    summary = summary or {}
    if any(k not in summary for k in NEW_FIELDS):
        return True
    steps = summary.get("flow_steps")
    return isinstance(steps, list) and any(isinstance(st, dict) and "detail" not in st for st in steps)


# ------------------------------------------------------------ 移行


def migrate(data):
    """旧形式 ({"entries": {"s:<sid>:<mtime>": ..., "i:<hash>": ...}}) を version 2 に変換する。

    セッションごとに最も新しい (mtime が大きい) 要約だけを残す。戻り値 (新しいデータ, 変換したか)。
    """
    data = data if isinstance(data, dict) else {}
    if data.get("version") == VERSION:
        out = empty()
        out.update(data)
        return out, False
    out = empty()
    for key, val in (data.get("entries") or {}).items():
        if not isinstance(val, dict):
            continue
        if key.startswith("s:"):
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            sid = parts[1]
            try:
                mtime = float(parts[2])
            except ValueError:
                continue
            cur = out["sessions"].get(sid)
            if cur is not None and cur["mtime"] >= mtime:
                continue
            out["sessions"][sid] = {
                "summary": clean_summary(val),
                "summarized_at": val.get("generated_at"),
                "n_user": None,
                "last_ts": None,
                "mtime": mtime,
            }
        elif key.startswith("i:"):
            out["legacy_items"][key[2:]] = val
    return out, True


def migrate_file(path):
    """ファイルが旧形式なら <path>.bak に退避してから変換して書き戻す。変換しなければ None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    new, changed = migrate(data)
    if not changed:
        return None
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    tmp = path + ".migrating"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return {
        "old_entries": len(data.get("entries") or {}),
        "sessions": len(new["sessions"]),
        "legacy_items": len(new["legacy_items"]),
    }


# ------------------------------------------------------------ セッション


def _is_busy(s):
    return ((s.get("live") or {}).get("status") or "").lower() == "busy"


def session_record(summary, s, at):
    return {
        "summary": clean_summary(summary),
        "summarized_at": at,
        "n_user": s.get("n_user"),
        "last_ts": s.get("last_ts"),
        "mtime": s.get("mtime"),
    }


def session_needs_summary(entry, s, now, settings, force=False):
    """セッションを要約し直すべきか。

    - 要約が無い / force: 要約する
    - NEW_FIELDS が無い古い要約: 間隔や稼働状態に関係なく 1 回だけ取り直す (取り直した要約には必ず入る)
    - 前回から llm_resummarize_min_interval_minutes 分経っていなければしない
    - 新しいユーザーの依頼が llm_resummarize_min_new_user_msgs 件以上: する
    - 終わっている / 待機中 (busy 以外) で、前回以降に何かメッセージがある: する
    """
    if force or not entry:
        return True
    if lacks_new_fields(entry.get("summary")):
        return True
    min_new = int(settings.get("llm_resummarize_min_new_user_msgs", DEFAULT_MIN_NEW_USER_MSGS))
    min_interval = float(settings.get("llm_resummarize_min_interval_minutes", DEFAULT_MIN_INTERVAL_MINUTES))
    at = entry.get("summarized_at") or 0
    if now - at < min_interval * 60:
        return False
    if entry.get("n_user") is None:
        # 旧形式から移行した要約: 件数が分からないので、書き込みの有無だけで判断する
        has_new = (s.get("mtime") or 0) > (entry.get("mtime") or 0) + 1
        return has_new and not _is_busy(s)
    new_user = (s.get("n_user") or 0) - (entry.get("n_user") or 0)
    if new_user >= min_new:
        return True
    cur_t = parse_iso(s.get("last_ts"))
    old_t = parse_iso(entry.get("last_ts"))
    has_new = new_user > 0 or (cur_t is not None and (old_t is None or cur_t > old_t))
    return has_new and not _is_busy(s)


# ------------------------------------------------------------ 案件


def ticket_member_stamp(summarized_at, manual_phase=None):
    """チケットのまとめを作り直すかの判定に使う、中の会話 1 件の印。

    会話の要約の時刻に、利用者が手で決めたフェーズを足す (会話を手で完了にしたら、まとめを作り直すため)。
    """
    return "%s|%s" % (summarized_at, manual_phase) if manual_phase else summarized_at


def item_record(summary, member_entries, at):
    """member_entries: {sessionId: {"summarized_at": ...}} (要約に使ったセッション要約)。"""
    return {
        "summary": clean_summary(summary),
        "summarized_at": at,
        "members": sorted(member_entries),
        "member_stamps": {sid: e.get("summarized_at") for sid, e in member_entries.items()},
    }


def item_needs_summary(entry, member_entries, ticket=False):
    """案件の統合要約を作り直すべきか: 構成セッションが変わったか、どれかの要約が作り直されたとき。

    NEW_FIELDS の無い古い統合要約も作り直す。ticket なら、TICKET_FIELDS の無い古いまとめも作り直す。
    """
    if not entry or lacks_new_fields(entry.get("summary")):
        return True
    if ticket and any(k not in (entry.get("summary") or {}) for k in TICKET_FIELDS):
        return True
    if sorted(member_entries) != sorted(entry.get("members") or []):
        return True
    stamps = entry.get("member_stamps") or {}
    return any(stamps.get(sid) != e.get("summarized_at") for sid, e in member_entries.items())


def legacy_merge_key(session_summaries, pr_titles):
    """旧形式の案件要約のキー (要約の内容と PR タイトルのハッシュ)。移行時の照合にだけ使う。"""
    payload = json.dumps({"s": session_summaries, "p": pr_titles}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def adopt_legacy_item(legacy_items, summaries, pr_titles, member_entries):
    """旧形式の案件要約 (内容ハッシュがキー) が今の案件に一致すれば、新形式のレコードにして返す。

    summaries: [(sessionId, summary)] (新しい順)。
    """
    if not legacy_items:
        return None
    h = legacy_merge_key([[sid, sm.get("title"), sm.get("next_action")] for sid, sm in summaries], list(pr_titles))
    val = legacy_items.get(h)
    if not val:
        return None
    return item_record(val, member_entries, val.get("generated_at"))
