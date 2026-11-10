"""作業 (案件) のフェーズを事実から導出する純粋ロジック。"""

import datetime

# 手動で設定できるのもこの一覧 (旧い「不要」は overrides.py が「完了」に読み替える)
PHASES = ["調査", "実装中", "レビュー待ち", "マージ済み", "デプロイ済み", "完了", "その他"]
LLM_PHASES = {"調査", "実装中", "レビュー待ち", "その他"}
DONE_PHASES = {"マージ済み", "デプロイ済み", "完了"}


def parse_iso(ts):
    """ISO8601 文字列を epoch 秒に変換する。失敗時は None。"""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def derive_phase(info, now, stale_days):
    """info: prs, branches, edited, has_sessions, llm_phase, last_activity, override_phase, reopened

    reopened (画面の「進行中に戻す」) なら、マージ済み・閉じた PR を除いて決める (開いている PR は使う)。
    PR のマージで完了の区分に入った作業を、手でフェーズを決めずに進行中に戻すため。
    """
    prs = info.get("prs") or []
    if info.get("reopened"):
        prs = [p for p in prs if p.get("state") == "OPEN"]
    changes_requested = any(
        p.get("state") == "OPEN" and p.get("reviewDecision") == "CHANGES_REQUESTED" for p in prs
    )
    phase = None
    source = None
    override = info.get("override_phase")
    if override in PHASES:
        phase, source = override, "手動"
    elif prs:
        open_prs = [p for p in prs if p.get("state") == "OPEN"]
        if open_prs:
            if any(not p.get("isDraft") for p in open_prs):
                phase = "レビュー待ち"
            else:
                phase = "実装中"
        elif any(p.get("state") == "MERGED" for p in prs):
            phase = "マージ済み"
        else:
            phase = "その他"
        source = "PR"
    if phase is None:
        branches = info.get("branches") or []
        if any((b.get("ahead") or 0) > 0 or b.get("dirty") for b in branches):
            phase, source = "実装中", "git"
        elif info.get("edited"):
            phase, source = "実装中", "編集"
        elif info.get("llm_phase") in LLM_PHASES:
            phase, source = info["llm_phase"], "LLM"
        else:
            phase, source = "調査", "会話"

    last = parse_iso(info.get("last_activity"))
    stale = bool(last is not None and phase not in DONE_PHASES and now - last > stale_days * 86400)
    return {"phase": phase, "source": source, "stale": stale, "changes_requested": changes_requested}
