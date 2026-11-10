"""収集した事実から、会話 1 つにつき 1 件の作業 (work item) を作る純粋ロジック。"""

import hashlib

from .classify import resolve_path
from . import summary_cache
from . import tickets as tk
from .overrides import FIELDS as OVERRIDE_FIELDS, normalize_record
from .phase import DONE_PHASES, PHASES, derive_phase, parse_iso

OTHER = "その他"  # targets.json の other_label の既定値 (実際の列名は classifier.other_label)
MAX_FILES = 80
# 画面に渡すタブの項目。window_ref / workspace_ref / surface_ref は cmux のときだけ値がある
TAB_FIELDS = ("id", "group", "window_ref", "workspace_ref", "workspace_title", "surface_ref", "title", "needs_input",
              "session_id", "cwd", "worktree_path", "branch", "tty")
NEEDS_INPUT_STATUSES = {"waiting", "needs_input", "needs-input", "permission", "input"}


def item_id_for_keys(keys):
    return hashlib.sha1(min(keys).encode("utf-8")).hexdigest()[:10]


def _pr(repo, number):
    return "pr:%s:%d" % (repo, int(number))


def _ss(sid):
    return "ss:%s" % sid


def _max_ts(values):
    best = None
    best_t = None
    for v in values:
        t = parse_iso(v)
        if t is not None and (best_t is None or t > best_t):
            best, best_t = v, t
    return best


def _pr_label(pr_files_kinds, repo, repo_kinds=None):
    """変更ファイルの種別から PR の表示名を決める。どのルールにも当たらなければリポジトリの default_kind を使う。"""
    kinds = set(pr_files_kinds)
    if not kinds and (repo_kinds or {}).get(repo):
        kinds = {repo_kinds[repo]}
    if "app" in kinds:
        return "アプリ PR"
    if kinds & {"infra-tf", "infra-k8s"}:
        return "インフラ PR"
    return "PR"


def _best_pr_title(prs):
    """表示用の PR タイトル: 未マージ > アプリ PR > 更新が新しいもの の順に選ぶ。"""
    cands = [p for p in prs if p.get("title")]
    if not cands:
        return None
    cands.sort(key=lambda p: (p.get("state") == "OPEN", p.get("label") == "アプリ PR",
                              parse_iso(p.get("updatedAt")) or 0), reverse=True)
    return cands[0]["title"]


def _session_key(s):
    return s.get("session_id") or s.get("file")


def is_session_override(ov, ss_key):
    """上書きの 1 件が、その会話 1 つだけのものか (会話の ID のキーが ss_key だけ)。

    会話が 2 つ以上だった作業の上書きは、どの会話のものか決まらないので当てはめない。
    """
    ss = {k for k in ov.get("keys") or [] if k.startswith("ss:")}
    return ss == {ss_key}


def _session_overrides(ss_key, override_items):
    """その会話の上書き設定を集める。同じ会話の記録が複数あれば、先にあるものを優先する。"""
    out = {}
    for ov in override_items:
        if not isinstance(ov, dict) or not is_session_override(ov, ss_key):
            continue
        # 旧い値 (phase 不要 / archived) は読むときに新しい値に読み替える
        ov, _changed = normalize_record(ov)
        for field in OVERRIDE_FIELDS:
            if field not in out and ov.get(field) not in (None, ""):
                out[field] = ov[field]
    return out


def _is_dedicated(res, repo_paths):
    """解決結果がメインのチェックアウトではなく専用 worktree を指しているか。"""
    root = repo_paths.get(res["repo"], "").rstrip("/")
    return bool(res.get("worktree")) and res["worktree"].rstrip("/") != root


def _session_edits(s):
    edits = s.get("edits")
    if edits is None:
        edits = [{"file": f, "cwd": s.get("cwd_last"), "branch": None} for f in s.get("files_edited") or []]
    return edits


def _edit_branch(e, repo_paths, worktrees):
    """編集したファイルのブランチを推定する。戻り値 (branch, repo)。"""
    res = resolve_path(e.get("file"), repo_paths, worktrees)
    if not res:
        return None, None
    if _is_dedicated(res, repo_paths):
        if res.get("branch"):
            return res["branch"], res["repo"]
    cres = resolve_path(e.get("cwd"), repo_paths, worktrees)
    # gitBranch は cwd のリポジトリのもの。編集先と同じチェックアウトのときだけ使う
    if cres and cres["repo"] == res["repo"] and cres.get("worktree") == res.get("worktree"):
        return e.get("branch") or res.get("branch"), res["repo"]
    return res.get("branch"), res["repo"]


def _uniq(rows, key):
    out, seen = [], set()
    for r in rows:
        k = key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _ticket_item(t, sids, raw, classifier, llm_items, now, stale_days):
    """チケット 1 つを作業 1 件にする。中の会話の作業 (members) を、古い順に持つ。"""
    found = [raw[sid] for sid in sids if sid in raw]
    found.sort(key=lambda r: (parse_iso(r["item"]["sessions"][0].get("first_ts")) or 0))
    members = [r["item"] for r in found]
    for m in members:
        m["ticket_id"] = t["id"]
    other = classifier.other_label

    hits, kinds_by_target = {}, {}
    for r in found:
        for target, n in r["hits"].items():
            hits[target] = hits.get(target, 0) + n
        for target, ks in r["kinds_by_target"].items():
            kinds_by_target.setdefault(target, set()).update(ks)
    if hits:
        order = {name: i for i, name in enumerate(classifier.target_names)}
        targets = sorted(hits, key=lambda x: (-hits[x], order.get(x, 99)))
    else:
        # 要約やタイトルの語から決めた会話の列を、多い順に使う
        count = {}
        for m in members:
            count[m["target"]] = count.get(m["target"], 0) + 1
        targets = sorted(count, key=lambda x: -count[x]) or [other]
    # 列を手で選んだチケットは、その列に固定する (列の設定から消えた名前なら、自動で決める)
    target_manual = bool(t.get("target")) and t["target"] in classifier.target_names + [other]
    if target_manual:
        targets = [t["target"]] + [x for x in targets if x != t["target"]]
    target = targets[0]

    prs_raw = _uniq([p for r in found for p in r["prs"]], lambda p: (p["repo"], p["number"]))
    pr_out = sorted(_uniq([p for m in members for p in m["prs"]], lambda p: (p["repo"], p["number"])),
                    key=lambda x: (x["repo"], x["number"]))
    branches_out = sorted(_uniq([b for m in members for b in m["branches"]], lambda b: (b["repo"], b["branch"])),
                          key=lambda x: (x["repo"], x["branch"]))
    sess_out = sorted([s for m in members for s in m["sessions"]],
                      key=lambda x: parse_iso(x.get("last_ts")) or 0, reverse=True)
    tabs = _uniq([x for m in members for x in m["tabs"]], lambda x: (x.get("id"), x.get("surface_ref"), x.get("tty")))
    files = []
    for m in members:
        for f in m["files"]:
            if f not in files and len(files) < MAX_FILES:
                files.append(f)
    sessions_raw = [{"last_ts": s.get("last_ts")} for s in sess_out]
    activity_ts = item_sort_ts(sessions_raw, prs_raw, branches_out)
    last_activity = _max_ts([m["last_activity"] for m in members])

    # 要約: 中の会話に要約があれば、チケットの id で引いたチケットのまとめを使う (会話が 1 つでも作る。
    # どういう話か・今どうなっているか・会話どうしのつながりは、チケットのまとめにしか無いため)。
    # まだ無ければ、いちばん新しい会話の要約を使う
    summaries = [(s["session_id"], s["summary"]) for s in sess_out if s.get("summary")]
    entry, refresh = None, False
    if sess_out:
        # 要約がまだ無い会話も入れる (入った会話が増えたら作り直す。その会話の要約ができたら、もう一度作り直す)
        manual = {m["sessions"][0]["session_id"]: m["phase"] for m in members
                  if m.get("sessions") and m.get("phase_source") == "手動"}
        member_stamps = {s["session_id"]: {"summarized_at": summary_cache.ticket_member_stamp(
            (s.get("summary") or {}).get("summarized_at"), manual.get(s["session_id"]))} for s in sess_out}
        entry = llm_items.get(t["id"])
        refresh = summary_cache.item_needs_summary(entry, member_stamps, ticket=True)
    merged = (entry or {}).get("summary")
    primary = merged or (summaries[0][1] if summaries else None)
    summarized_at = entry.get("summarized_at") if merged else (primary or {}).get("summarized_at")

    info = {
        "prs": prs_raw,
        "branches": branches_out,
        "edited": any(r["edited"] for r in found),
        "has_sessions": bool(members),
        "llm_phase": (primary or {}).get("phase_guess"),
        "last_activity": last_activity,
        "override_phase": t.get("phase") if t.get("phase") in PHASES else None,
        "reopened": bool(t.get("reopened")),
    }
    ph = derive_phase(info, now=now, stale_days=stale_days)
    if ph["phase"] == "マージ済み" and ph["source"] == "PR":
        # 最後の PR のマージより後に始めた会話があれば、続きの作業中とみなす (PR を除いて決め直す)
        merged_at = _max_ts([p.get("mergedAt") for p in prs_raw])
        t_merge = parse_iso(merged_at)
        if t_merge is not None and any((parse_iso(s.get("first_ts")) or 0) > t_merge for s in sess_out):
            again = derive_phase(dict(info, prs=[]), now=now, stale_days=stale_days)
            ph = dict(again, changes_requested=ph["changes_requested"])

    # 手で完了の区分にした会話は、チケットの「指示待ち」に数えない (利用者の中では終わった作業のため)
    needs_input = any(m["needs_input"] for m in members if not (m.get("phase_source") == "手動" and m["phase"] in DONE_PHASES))
    if sess_out and (not merged or refresh):
        llm_status = "pending"
    elif summaries:
        llm_status = "done"
    elif members:
        llm_status = "pending"
    else:
        llm_status = "none"

    return {
        "id": t["id"],
        "kind": "ticket",
        "ticket_id": t["id"],
        "ticket": {k: t.get(k) or "" for k in ("name", "url", "project", "memo")},
        "missing_sessions": len([sid for sid in sids if sid not in raw]),
        "keys": [tk.ticket_key(t["id"])],
        "title": t.get("name") or tk.DEFAULT_NAME,
        "title_source": "手動",
        "target": target,
        "target_manual": target_manual,
        "targets": targets,
        "kinds": sorted(kinds_by_target.get(target, set())),
        "repo_branches": [{"repo": b["repo"], "branch": b["branch"]} for b in branches_out],
        "branches": branches_out,
        "prs": pr_out,
        "sessions": sess_out,
        "members": members,
        "tabs": tabs,
        "files": files,
        "last_activity": last_activity,
        "phase": ph["phase"],
        "phase_source": ph["source"],
        "stale": ph["stale"],
        "changes_requested": ph["changes_requested"],
        "needs_input": needs_input,
        "goal": (primary or {}).get("goal"),
        "done_so_far": (primary or {}).get("done_so_far"),
        "next_action": (primary or {}).get("next_action"),
        "blocked_question": (primary or {}).get("blocked_question"),
        **{k: (primary or {}).get(k) for k in summary_cache.NEW_FIELDS},
        **{k: (merged or {}).get(k) for k in summary_cache.TICKET_FIELDS},
        "memo": t.get("memo") or "",
        "hidden": bool(t.get("hidden")),
        "reopened": bool(t.get("reopened")),
        "llm_item_refresh": refresh,
        "summarized_at": summarized_at,
        "activity_ts": activity_ts,
        "llm_status": llm_status,
    }


def item_sort_ts(sessions, prs, branches):
    """カードの並び順に使う時刻 (ISO)。セッションがあれば最後のやりとり、なければ PR / ブランチの更新。"""
    ts = _max_ts([s.get("last_ts") for s in sessions])
    if ts:
        return ts
    return _max_ts([p.get("updatedAt") for p in prs] + [b.get("date") for b in branches])


def item_sort_key(item):
    """新しいやりとりのある案件を先頭に。フェーズや要対応は順序に影響させない。"""
    return (-(parse_iso(item.get("activity_ts")) or 0), item["id"])


def build_items(facts, classifier, settings, overrides, llm, tickets=None):
    """会話 (Claude Code のセッション) 1 つにつき作業を 1 件作る。会話どうしは自動でつながない。

    PR は、その PR を作った会話 (prs[].created) の作業にだけ付ける。ブランチは、その会話が専用の worktree で
    作業したか、編集したときのブランチを、表示のために付けるだけ。会話の無い PR やブランチは出さない。
    チケット (tickets.json) に入った会話は、ボードには出さず、チケットの作業 1 件にまとめて出す。
    """
    now = facts.get("now")
    repos_info = facts.get("repos") or {}
    repo_paths = {name: r["path"] for name, r in repos_info.items()}
    worktrees = {name: r.get("worktrees") or [] for name, r in repos_info.items()}
    default_branch = {name: r.get("default_branch") or "main" for name, r in repos_info.items()}
    stale_days = settings.get("stale_days", 3)
    llm_sessions = (llm or {}).get("sessions") or {}
    other = classifier.other_label

    def is_feature_branch(repo, branch):
        return bool(branch) and branch not in ("HEAD", default_branch.get(repo, "main"))

    pr_by_key = {}
    for p in facts.get("prs") or []:
        if p.get("repo") in repo_paths:
            pr_by_key[_pr(p["repo"], p["number"])] = p

    sessions = sorted(facts.get("sessions") or [], key=lambda s: _session_key(s) or "")
    sids = {_session_key(s) for s in sessions}

    # タブ: 会話と結び付いたもの (collectors が tty / pid / cwd で session_id を付けたもの) だけを、その会話に付ける
    tabs_by_sid = {}
    unlinked = []
    for tab in facts.get("tabs") or []:
        if tab.get("session_id") and tab["session_id"] in sids:
            tabs_by_sid.setdefault(tab["session_id"], []).append(tab)
            continue
        res = resolve_path(tab.get("cwd") or tab.get("worktree_path"), repo_paths, worktrees)
        if not res:
            continue
        hits = classifier.classify(res["repo"], res["rel"])
        target = hits[0][0] if hits else None
        if not target:
            kw = classifier.keyword_targets(tab.get("workspace_title") or "")
            target = kw[0] if kw else other
        t2 = dict(tab)
        t2["target"] = target
        t2["repo"] = res["repo"]
        unlinked.append(t2)

    override_items = (overrides or {}).get("items") or []
    items = []
    raw = {}  # 会話の ID -> チケットにまとめるときに使う材料
    for s in sessions:
        sid = _session_key(s)
        keys = [_ss(sid)]

        # この会話で作った PR
        item_prs = []
        for ref in s.get("prs") or []:
            if not ref.get("created") or ref.get("repo") not in repo_paths:
                continue
            k = _pr(ref["repo"], ref["number"])
            p = pr_by_key.get(k) or {
                "repo": ref["repo"], "number": ref["number"], "url": ref.get("url"),
                "title": None, "state": None, "isDraft": False, "files": [], "headRefName": None,
            }
            if p not in item_prs:
                item_prs.append(p)

        # この会話で触ったブランチ (専用の worktree での作業と、編集したときのブランチ)。表示するだけ
        touched = []
        for loc in s.get("locations") or []:
            res = resolve_path(loc.get("cwd"), repo_paths, worktrees)
            if not res or not _is_dedicated(res, repo_paths):
                continue
            branch = res.get("branch") or loc.get("branch")
            if is_feature_branch(res["repo"], branch):
                touched.append((res["repo"], branch))
        for e in _session_edits(s):
            branch, repo = _edit_branch(e, repo_paths, worktrees)
            if repo and is_feature_branch(repo, branch):
                touched.append((repo, branch))
        item_branches = [{"repo": r, "branch": b} for r, b in dict.fromkeys(touched)]

        # 対象の判定
        hits = {}
        kinds_by_target = {}
        files = []

        def hit(repo, rel, record_file=False):
            for target, kind in classifier.classify(repo, rel):
                hits[target] = hits.get(target, 0) + 1
                kinds_by_target.setdefault(target, set()).add(kind)
            if record_file:
                f = "%s:%s" % (repo, rel)
                if f not in files and len(files) < MAX_FILES:
                    files.append(f)

        edited = False
        for loc in s.get("locations") or []:
            res = resolve_path(loc.get("cwd"), repo_paths, worktrees)
            if res:
                hit(res["repo"], res["rel"])
        for fp in s.get("files_edited") or []:
            res = resolve_path(fp, repo_paths, worktrees)
            if res:
                edited = True
                hit(res["repo"], res["rel"], record_file=True)
        res = resolve_path(s.get("cwd_last"), repo_paths, worktrees)
        summary = llm_sessions.get(s.get("session_id"))
        sess_out = [{
            "session_id": s.get("session_id"),
            "cwd": s.get("cwd_last"),
            "repo": res["repo"] if res else None,
            "rel": res["rel"] if res else None,
            "branch": s.get("branch_last"),
            "ai_title": s.get("ai_title"),
            "first_prompt": (s.get("first_prompts") or [None])[0],
            "last_prompt": s.get("last_prompt"),
            "first_ts": s.get("first_ts"),
            "last_ts": s.get("last_ts"),
            "live": s.get("live"),
            "summary": summary,
            "files_edited": len(s.get("files_edited") or []),
        }]

        pr_out = []
        for p in item_prs:
            pkinds = []
            for f in p.get("files") or []:
                path = f.get("path") if isinstance(f, dict) else f
                for target, kind in classifier.classify(p["repo"], path):
                    pkinds.append(kind)
                    hits[target] = hits.get(target, 0) + 1
                    kinds_by_target.setdefault(target, set()).add(kind)
                fkey = "%s:%s" % (p["repo"], path)
                if fkey not in files and len(files) < MAX_FILES:
                    files.append(fkey)
            pr_out.append({
                "repo": p["repo"],
                "number": p["number"],
                "title": p.get("title"),
                "url": p.get("url"),
                "state": p.get("state"),
                "isDraft": bool(p.get("isDraft")),
                "reviewDecision": p.get("reviewDecision") or "",
                "updatedAt": p.get("updatedAt"),
                "mergedAt": p.get("mergedAt"),
                "headRefName": p.get("headRefName"),
                "label": _pr_label(pkinds, p["repo"], classifier.repo_kinds),
            })
        pr_out.sort(key=lambda x: (x["repo"], x["number"]))

        if hits:
            order = {name: i for i, name in enumerate(classifier.target_names)}
            targets = sorted(hits, key=lambda t: (-hits[t], order.get(t, 99)))
            target = targets[0]
        else:
            text = " ".join(
                [b["branch"] for b in item_branches] + [p.get("headRefName") or "" for p in item_prs]
                + [p.get("title") or "" for p in item_prs]
                + [s.get("ai_title") or "", (summary or {}).get("title") or ""]
            )
            kw = classifier.keyword_targets(text)
            target = kw[0] if kw else other
            targets = kw or [other]
        kinds = sorted(kinds_by_target.get(target, set()))

        branches_out = []
        for b in item_branches:
            info = (repos_info.get(b["repo"], {}).get("branches") or {}).get(b["branch"], {})
            branches_out.append({
                "repo": b["repo"],
                "branch": b["branch"],
                "ahead": info.get("ahead"),
                "dirty": bool(info.get("dirty")),
                "worktree": info.get("worktree"),
                "date": info.get("date"),
            })
        branches_out.sort(key=lambda x: (x["repo"], x["branch"]))

        # 並び順: 会話の最後のやりとりの時刻
        activity_ts = item_sort_ts([s], item_prs, branches_out)
        last_activity = _max_ts(
            [s.get("last_ts")] + [p.get("updatedAt") for p in item_prs] + [b.get("date") for b in branches_out]
        )

        ov = _session_overrides(keys[0], override_items)
        ph = derive_phase(
            {
                "prs": item_prs,
                "branches": branches_out,
                "edited": edited,
                "has_sessions": True,
                "llm_phase": (summary or {}).get("phase_guess"),
                "last_activity": last_activity,
                "override_phase": ov.get("phase") if ov.get("phase") in PHASES else None,
                "reopened": bool(ov.get("reopened")),
            },
            now=now,
            stale_days=stale_days,
        )

        title, title_source = None, None
        candidates = [
            (ov.get("title"), "手動"),
            ((summary or {}).get("title"), "LLM"),
            (_best_pr_title(pr_out), "PR"),
            (s.get("ai_title"), "セッション"),
            (branches_out[0]["branch"] if branches_out else None, "ブランチ"),
            (sess_out[0]["first_prompt"], "プロンプト"),
        ]
        for value, src in candidates:
            if value:
                title, title_source = value, src
                break
        if not title:
            title, title_source = "(無題)", None

        item_tabs = tabs_by_sid.get(sid, [])
        needs_input = any(t.get("needs_input") for t in item_tabs) or (
            ((s.get("live") or {}).get("status") or "").lower() in NEEDS_INPUT_STATUSES)

        item = {
            "id": item_id_for_keys(keys),
            "kind": "session",
            "ticket_id": None,
            "keys": keys,
            "title": title,
            "title_source": title_source,
            "target": target,
            "targets": targets,
            "kinds": kinds,
            "repo_branches": [{"repo": b["repo"], "branch": b["branch"]} for b in branches_out],
            "branches": branches_out,
            "prs": pr_out,
            "sessions": sess_out,
            "tabs": [
                {k: t.get(k) for k in TAB_FIELDS}
                for t in item_tabs
            ],
            "files": files,
            "last_activity": last_activity,
            "phase": ph["phase"],
            "phase_source": ph["source"],
            "stale": ph["stale"],
            "changes_requested": ph["changes_requested"],
            "needs_input": needs_input,
            "goal": (summary or {}).get("goal"),
            "done_so_far": (summary or {}).get("done_so_far"),
            "next_action": (summary or {}).get("next_action"),
            "blocked_question": (summary or {}).get("blocked_question"),
            # 6 章の項目。古い要約には無いので None になる (画面は無くても描ける)
            **{k: (summary or {}).get(k) for k in summary_cache.NEW_FIELDS},
            "memo": ov.get("memo") or "",
            "hidden": bool(ov.get("hidden")),
            "reopened": bool(ov.get("reopened")),
            "summarized_at": (summary or {}).get("summarized_at"),
            "activity_ts": activity_ts,
            "llm_status": "done" if summary else "pending",
        }
        items.append(item)
        raw[sid] = {"item": item, "prs": item_prs, "edited": edited, "summary": summary, "hits": hits,
                    "kinds_by_target": kinds_by_target}

    if tickets:
        in_ticket = tk.ticket_of_session(tickets)
        items = [it for it in items if _session_key(it["sessions"][0]) not in in_ticket]
        for t in tk.all_tickets(tickets):
            sids = [sid for sid in dict.fromkeys(t.get("session_ids") or []) if in_ticket.get(sid) == t["id"]]
            items.append(_ticket_item(t, sids, raw, classifier, (llm or {}).get("items") or {}, now, stale_days))

    items.sort(key=item_sort_key)

    summary = {"phases": {p: 0 for p in PHASES}, "needs_input": 0, "stale": 0, "changes_requested": 0,
               "attention": 0, "total": 0}
    for it in items:
        if it["hidden"]:
            continue
        summary["total"] += 1
        summary["phases"][it["phase"]] = summary["phases"].get(it["phase"], 0) + 1
        # 入力待ちの Claude はフェーズに関係なく要対応。放置・修正依頼は終わっていないものだけ
        open_item = it["phase"] not in DONE_PHASES
        stale = it["stale"] and open_item
        changes = it["changes_requested"] and open_item
        summary["needs_input"] += int(it["needs_input"])
        summary["stale"] += int(stale)
        summary["changes_requested"] += int(changes)
        if it["needs_input"] or stale or changes:
            summary["attention"] += 1

    return {"items": items, "unlinked_tabs": unlinked, "summary": summary}
