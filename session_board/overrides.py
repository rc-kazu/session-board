"""手動の上書き (overrides.json) の形をそろえる。

利用者が設定できる状態は「完了」(phase の上書き) と「非表示」(hidden) だけ。
旧い版が書いた値は読むときに次のように読み替え、書くときは新しい値だけを書く。

- phase "不要" -> "完了"
- archived: true -> hidden: true (archived の項目は消す)
"""

import json
import os
import shutil
import time

LEGACY_PHASES = {"不要": "完了"}
FIELDS = ("title", "phase", "memo", "hidden")


def normalize_record(rec):
    """上書きの 1 件を新しい形にする。戻り値 (新しい dict, 変えたか)。元の dict は変えない。"""
    if not isinstance(rec, dict):
        return rec, False
    out = dict(rec)
    changed = False
    if out.get("phase") in LEGACY_PHASES:
        out["phase"] = LEGACY_PHASES[out["phase"]]
        changed = True
    if "archived" in out:
        if out.pop("archived"):
            out["hidden"] = True
        changed = True
    return out, changed


def normalize(data):
    """overrides.json の中身全体。戻り値 (新しい dict, 数え上げ {phase, archived, records})。"""
    stats = {"phase": 0, "archived": 0, "records": 0}
    if not isinstance(data, dict):
        return data, stats
    out = dict(data)
    items = []
    for rec in data.get("items") or []:
        if isinstance(rec, dict):
            if rec.get("phase") in LEGACY_PHASES:
                stats["phase"] += 1
            if rec.get("archived"):
                stats["archived"] += 1
        new, changed = normalize_record(rec)
        stats["records"] += int(changed)
        items.append(new)
    if "items" in data:
        out["items"] = items
    return out, stats


def migrate_file(path, now=None):
    """overrides.json を新しい形に書き換える。変える必要が無ければ None。

    書き換える前に元のファイルを「overrides.json.bak-YYYYmmdd-HHMMSS」として残す。
    戻り値 {phase, archived, records, backup}。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    new, stats = normalize(data)
    if not stats["records"]:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    backup = "%s.bak-%s" % (path, stamp)
    n = 1
    while os.path.lexists(backup):
        n += 1
        backup = "%s.bak-%s-%d" % (path, stamp, n)
    shutil.copy2(path, backup)
    tmp = path + ".tmp-migrate"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return dict(stats, backup=backup)
