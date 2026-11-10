"""ローカルの事実を収集する (読み取り専用)。外部コマンドの出力解析は純粋関数に分けている。"""

import concurrent.futures
import glob
import json
import os
import re
import subprocess
import time

from . import report
from .aliases import alias_day_session, alias_live, alias_session, old_forms
from .phase import parse_iso
from .terminals import make_terminal
from .terminals.cmux import flatten_cmux_tree, parse_sidebar_state  # noqa: F401 (旧い場所からの import 用)
from .transcript import parse_file

def claude_dir(env=None):
    """Claude Code の設定・記録の場所。CLAUDE_CONFIG_DIR があればそれ、無ければ ~/.claude。"""
    env = os.environ if env is None else env
    d = env.get("CLAUDE_CONFIG_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude")


CLAUDE_DIR = claude_dir()
PR_FIELDS = ("number,title,url,state,headRefName,baseRefName,isDraft,createdAt,mergedAt,closedAt,updatedAt,files,"
             "reviewDecision")


# git status などがリポジトリの index を書き換えないようにする (収集は読み取り専用)
_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GH_PROMPT_DISABLED="1")


def run(cmd, timeout=30, cwd=None):
    """コマンドを実行して stdout を返す。失敗時は None。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


# ---------------------------------------------------------------- 解析 (純粋)

def parse_worktree_porcelain(text):
    out = []
    cur = None
    for line in (text or "").splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": None, "head": None}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            ref = line[7:]
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            cur["branch"] = None
    return out


def parse_for_each_ref(text):
    """`%(refname:short)|%(committerdate:iso-strict)|%(upstream:short)|%(upstream:track)` を解析する。"""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, date, upstream, track = parts[0], parts[1], parts[2], "|".join(parts[3:])
        ahead = behind = 0
        m = re.search(r"ahead (\d+)", track)
        if m:
            ahead = int(m.group(1))
        m = re.search(r"behind (\d+)", track)
        if m:
            behind = int(m.group(1))
        out[name] = {
            "date": date,
            "upstream": upstream or None,
            "upstream_gone": "gone" in track,
            "ahead_upstream": ahead,
            "behind_upstream": behind,
        }
    return out


def parse_ps(text):
    """`ps -A -o pid=,tty=,stat=,comm=` -> {pid: {...}}"""
    procs = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        procs[pid] = {"pid": pid, "tty": parts[1], "stat": parts[2], "comm": parts[3].strip()}
    return procs


def parse_lsof_cwd(text):
    out = {}
    pid = None
    for line in (text or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def encode_project_dir(path):
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


# ---------------------------------------------------------------- 収集

def collect_processes():
    procs = parse_ps(run(["ps", "-A", "-o", "pid=,tty=,stat=,comm="], timeout=10))
    shells = [pid for pid, p in procs.items() if p["comm"].lstrip("-").endswith(("zsh", "bash", "fish"))]
    cwds = {}
    if shells:
        out = run(["lsof", "-a", "-d", "cwd", "-p", ",".join(str(p) for p in shells), "-Fpn"], timeout=15)
        cwds = parse_lsof_cwd(out)
    return procs, cwds


def tty_shell_cwd(procs, cwds):
    """tty ごとにシェルのカレントディレクトリを推定する (前面のシェルを優先)。"""
    by_tty = {}
    for pid, p in sorted(procs.items()):
        if pid not in cwds:
            continue
        tty = p["tty"]
        if tty in ("??", "-"):
            continue
        prev = by_tty.get(tty)
        fg = "+" in p["stat"]
        if prev is None or (fg and not prev[1]):
            by_tty[tty] = (cwds[pid], fg)
    return {tty: v[0] for tty, v in by_tty.items()}


# ~/.claude/sessions/<pid>.json の status。Claude Code 2.1 系で見つかる値は busy / idle / waiting (waitingFor 付き)
_STATUS_NEEDS_INPUT = {"waiting": True, "busy": False, "idle": False}


def needs_input_from_status(status):
    """セッションの status から「指示待ち」かを決める。分からない値は None。

    waiting (許可や質問への回答を待っている) だけを True にする。idle は返答を終えて止まっているだけで、
    これを指示待ちにすると開いている全セッションに付いてしまうので False にする (控えめに判定する)。
    """
    if not isinstance(status, str):
        return None
    return _STATUS_NEEDS_INPUT.get(status.lower())


def link_tabs(tabs, live, shell_cwd, adapter_needs_input):
    """タブに Claude セッションを結び付けた新しいタブの一覧を返す (入力は変更しない)。

    live: {sessionId: {pid, tty, status, cwd}}。結び付け方は強い順に
      1. tty が同じ  2. pid が同じ  3. cwd (無ければ worktree_path) が同じ稼働中のセッションが 1 つだけで、
         その cwd のタブも 1 つだけ
    1 つのセッションは 1 つのタブにだけ結び付ける。
    needs_input: アダプタが知らせるとき (adapter_needs_input) は、結び付いていて busy でないタブにだけ付ける。
    知らせないアダプタでは、結び付いたセッションの status から決める (needs_input_from_status)。
    """
    by_tty, by_pid, by_cwd = {}, {}, {}
    for sid, lv in live.items():
        if lv.get("tty"):
            by_tty.setdefault(lv["tty"], sid)
        if lv.get("pid") is not None:
            by_pid.setdefault(lv["pid"], sid)
        if lv.get("cwd"):
            by_cwd.setdefault(lv["cwd"], []).append(sid)

    def places(t):
        return [p for p in dict.fromkeys([t.get("cwd"), t.get("worktree_path")]) if p]

    tab_cwd_count = {}
    for t in tabs:
        for p in places(t):
            tab_cwd_count[p] = tab_cwd_count.get(p, 0) + 1
    used = set()
    out = []
    for t in tabs:
        sid = None
        cand = by_tty.get(t.get("tty")) if t.get("tty") else None
        if cand and cand not in used:
            sid = cand
        if sid is None and t.get("pid") is not None:
            cand = by_pid.get(t["pid"])
            if cand and cand not in used:
                sid = cand
        if sid is None:
            for p in places(t):
                sids = by_cwd.get(p) or []
                if len(sids) == 1 and tab_cwd_count.get(p) == 1 and sids[0] not in used:
                    sid = sids[0]
                    break
        if sid:
            used.add(sid)
        lv = live.get(sid) or {}
        tab = dict(t)
        tab["session_id"] = sid
        tab["cwd"] = lv.get("cwd") or shell_cwd.get(t.get("tty")) or t.get("cwd") or t.get("worktree_path")
        tab["claude_status"] = lv.get("status")
        if adapter_needs_input:
            tab["needs_input"] = bool(sid) and bool(t.get("needs_input")) and lv.get("status") != "busy"
        else:
            tab["needs_input"] = needs_input_from_status(lv.get("status")) if sid else None
        out.append(tab)
    return out


def collect_live_sessions(procs, aliases=()):
    """稼働中のセッション。cwd は path_aliases (aliases) で読み替える。"""
    out = {}
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        if not isinstance(pid, int) or pid not in procs:
            continue
        sid = d.get("sessionId")
        if not sid:
            continue
        tty = procs[pid]["tty"]
        out[sid] = alias_live({
            "pid": pid,
            "tty": None if tty in ("??", "-") else tty,
            "status": d.get("status"),
            "name": d.get("name"),
            "cwd": d.get("cwd"),
            "updated_at": d.get("updatedAt"),
        }, aliases)
    return out


class TranscriptCache:
    def __init__(self):
        self.cache = {}

    def collect(self, repo_paths, window_days, now, aliases=()):
        """repo_paths の下で動いたセッション。記録のパスは path_aliases (aliases) で読み替えてから判定する。

        キャッシュには読み替える前の解析結果を持ち、返すものだけを読み替える。projects のディレクトリ名は
        今の場所の名前に加えて、読み替える前の古い場所の名前も探す (ディレクトリを改名していない場合)。
        """
        roots = list(repo_paths.values())
        for p in list(roots):
            roots.extend(old_forms(p, aliases))
        prefixes = [encode_project_dir(p) for p in roots]
        cutoff = now - window_days * 86400
        sessions = []
        seen = set()
        for d in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*")):
            name = os.path.basename(d)
            if not any(name == p or name.startswith(p + "-") for p in prefixes):
                continue
            for path in glob.glob(os.path.join(d, "*.jsonl")):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                key = (st.st_mtime, st.st_size)
                cached = self.cache.get(path)
                if cached and cached[0] == key:
                    s = cached[1]
                else:
                    try:
                        s = parse_file(path)
                    except OSError:
                        continue
                    self.cache[path] = (key, s)
                if not s.get("session_id") or s["session_id"] in seen:
                    continue
                last = parse_iso(s.get("last_ts"))
                if last is None or last < cutoff:
                    continue
                if not (s.get("first_prompts") or s.get("files_edited")):
                    continue
                s = alias_session(s, aliases)
                cwds = [loc["cwd"] for loc in s.get("locations") or []]
                if not any(_under_any(c, repo_paths.values()) for c in cwds):
                    continue
                seen.add(s["session_id"])
                item = dict(s)
                item["file"] = path
                item["mtime"] = st.st_mtime
                sessions.append(item)
        return sessions


def _under_any(path, roots):
    for r in roots:
        if path == r or path.startswith(r.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------- 作業報告 (1 日分)

def scan_day_sessions(projects_dir, start, end, exclude=(), aliases=()):
    """~/.claude/projects 配下の全セッションから [start, end) にやりとりがあったものを返す。

    追跡対象のリポジトリに限らない (その日の作業全体を報告するため)。同じセッション ID は内容の多い方を使う。
    cwd と編集したファイルは path_aliases (aliases) で読み替える。
    """
    found = {}
    for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < start:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                s = report.parse_day_lines(f, start, end)
        except OSError:
            continue
        if not s or not s.get("session_id"):
            continue
        s = alias_day_session(s, aliases)
        cwd = s.get("cwd") or ""
        if any(cwd == x or cwd.startswith(x.rstrip("/") + "/") for x in exclude):
            continue
        s["file"] = path
        prev = found.get(s["session_id"])
        size = len(s["prompts"]) + len(s["assistant"]) + len(s["files"])
        if prev is None or size > len(prev["prompts"]) + len(prev["assistant"]) + len(prev["files"]):
            found[s["session_id"]] = s
    return list(found.values())


def git_common_root(path, cache=None):
    """path が属するリポジトリ本体 (worktree なら元のクローン) のルート。取れなければ None。"""
    if not path or not os.path.isdir(path):
        return None
    if cache is not None and path in cache:
        return cache[path]
    out = run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"], timeout=10)
    root = None
    if out and out.strip():
        d = out.strip().rstrip("/")
        root = os.path.dirname(d) if os.path.basename(d) == ".git" else d
    if cache is not None:
        cache[path] = root
    return root


def collect_commits(repo_paths, start, end, classifier):
    """追跡リポジトリで、その日に自分 (git config user.email) が author のコミット。"""
    since = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start - 86400))
    out = []
    seen = set()
    for repo, path in repo_paths.items():
        if not os.path.isdir(path):
            continue
        email = (run(["git", "-C", path, "config", "user.email"], timeout=10) or "").strip()
        if not email:
            continue
        text = run(["git", "-C", path, "log", "--all", "--no-merges", "--since=" + since,
                    "--author=" + email, "--format=%x1e%H%x1f%aI%x1f%s", "--name-only"], timeout=30)
        for c in report.parse_git_log(text, start, end):
            if c["hash"] in seen:
                continue
            seen.add(c["hash"])
            out.append({"repo": repo, "hash": c["hash"][:10], "ts": c["ts"], "subject": c["subject"],
                        "project": report.commit_project(repo, c["files"], classifier)})
    out.sort(key=lambda c: c["ts"])
    return out


def collect_git_repo(name, path, window_days, now):
    info = {"path": path, "default_branch": "main", "worktrees": [], "branches": {}, "error": None}
    if not os.path.isdir(path):
        info["error"] = "クローンが見つかりません: " + path
        return info
    head = run(["git", "-C", path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=10)
    if head and head.strip().startswith("origin/"):
        info["default_branch"] = head.strip()[len("origin/"):]
    default = info["default_branch"]
    info["worktrees"] = parse_worktree_porcelain(run(["git", "-C", path, "worktree", "list", "--porcelain"]))
    refs = parse_for_each_ref(run([
        "git", "-C", path, "for-each-ref", "refs/heads",
        "--format=%(refname:short)|%(committerdate:iso-strict)|%(upstream:short)|%(upstream:track)",
    ]))
    wt_by_branch = {w["branch"]: w["path"] for w in info["worktrees"] if w.get("branch")}
    cutoff = now - window_days * 86400
    for br, r in refs.items():
        t = parse_iso(r["date"])
        if br == default or t is None or t < cutoff:
            continue
        r["worktree"] = wt_by_branch.get(br)
        cnt = run(["git", "-C", path, "rev-list", "--count", "origin/%s..%s" % (default, br)], timeout=10)
        r["ahead"] = int(cnt.strip()) if cnt and cnt.strip().isdigit() else None
        # 既にリモートと同じなら「ローカルのみのコミット」とは見なさない
        if r["ahead"] and r["upstream"] and not r["upstream_gone"] and r["ahead_upstream"] == 0:
            r["unpushed"] = 0
        else:
            r["unpushed"] = r["ahead_upstream"] if r["upstream"] else r["ahead"]
        r["files"] = []
        if r["ahead"] and r["ahead"] <= 300:
            diff = run(["git", "-C", path, "diff", "--name-only", "origin/%s...%s" % (default, br)], timeout=15)
            r["files"] = (diff or "").splitlines()[:300]
        r["dirty"] = False
        if r["worktree"] and r["worktree"] != path:
            st = run(["git", "-C", r["worktree"], "status", "--porcelain"], timeout=15)
            r["dirty"] = bool(st and st.strip())
        info["branches"][br] = r
    # main チェックアウトで別ブランチを開いている場合の dirty
    for w in info["worktrees"]:
        if w["path"] == path and w.get("branch") and w["branch"] != default and w["branch"] in info["branches"]:
            st = run(["git", "-C", path, "status", "--porcelain"], timeout=15)
            info["branches"][w["branch"]]["dirty"] = bool(st and st.strip())
    return info


def collect_git(repo_paths, window_days, now):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {name: ex.submit(collect_git_repo, name, path, window_days, now) for name, path in repo_paths.items()}
        return {name: f.result() for name, f in futs.items()}


class GhCache:
    def __init__(self):
        self.lists = {}  # repo -> (fetched_at, prs)
        self.views = {}  # (repo, number) -> (fetched_at, pr)
        self.errors = []

    def collect(self, owners, repos, refs, window_days, now, max_age, force):
        """owners: {リポジトリ名: GitHub の owner}。owner の無いリポジトリは GitHub から取らない。"""
        self.errors = []
        repos = [r for r in repos if owners.get(r)]

        def fetch_list(repo):
            cmd = ["gh", "pr", "list", "--repo", "%s/%s" % (owners[repo], repo), "--author", "@me", "--state", "all",
                   "--limit", "50", "--json", PR_FIELDS]
            out = run(cmd, timeout=60)
            if out is None:
                # GitHub API の一時的な失敗に備えて 1 回だけ再試行する
                time.sleep(2)
                out = run(cmd, timeout=60)
            if out is None:
                return repo, None
            try:
                return repo, json.loads(out)
            except ValueError:
                return repo, None

        todo = [r for r in repos if force or r not in self.lists or now - self.lists[r][0] > max_age]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for repo, prs in ex.map(fetch_list, todo):
                if prs is None:
                    self.errors.append("gh pr list に失敗: " + repo)
                    continue
                for p in prs:
                    p["repo"] = repo
                self.lists[repo] = (now, prs)

        known = {}
        for repo in repos:
            for p in (self.lists.get(repo) or (0, []))[1]:
                known[(repo, p["number"])] = p

        missing = []
        for ref in refs:
            key = (ref["repo"], int(ref["number"]))
            if ref["repo"] not in repos or key in known:
                continue
            cached = self.views.get(key)
            if cached and (cached[1].get("state") != "OPEN" or now - cached[0] <= max_age) and not force:
                continue
            if key not in missing:
                missing.append(key)

        def fetch_view(key):
            repo, num = key
            out = run(["gh", "pr", "view", str(num), "--repo", "%s/%s" % (owners[repo], repo), "--json", PR_FIELDS],
                      timeout=60)
            if out is None:
                return key, None
            try:
                return key, json.loads(out)
            except ValueError:
                return key, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for key, p in ex.map(fetch_view, missing[:40]):
                if p is None:
                    continue
                p["repo"] = key[0]
                self.views[key] = (now, p)

        cutoff = now - window_days * 86400
        result = {}
        referenced = {(r["repo"], int(r["number"])) for r in refs}
        created = {(r["repo"], int(r["number"])) for r in refs if r.get("created")}

        def wanted(key, p):
            t = parse_iso(p.get("updatedAt"))
            return p.get("state") == "OPEN" or key in created or (t is not None and t >= cutoff)

        for key, p in known.items():
            if wanted(key, p):
                result[key] = p
        for key, (_, p) in self.views.items():
            if key in referenced and key not in result and wanted(key, p):
                result[key] = p
        out = []
        for p in result.values():
            q = dict(p)
            q["files"] = [f.get("path") for f in (p.get("files") or []) if isinstance(f, dict)]
            out.append(q)
        return out


def pr_refs(sessions, owners):
    """トランスクリプトに出てきた PR のうち、設定した owner/リポジトリのものだけ (大文字小文字は区別しない)。"""
    out = []
    for s in sessions:
        for p in s.get("prs") or []:
            owner = owners.get(p.get("repo"))
            if owner and str(p.get("org") or "").lower() == owner.lower():
                out.append(p)
    return out


def collect_all(targets, settings, transcripts, gh, force_gh, progress, terminal=None):
    """全ての事実を収集して grouping に渡す形にまとめる。terminal はターミナルのアダプタ (terminals)。"""
    now = time.time()
    repo_paths = targets["repos"]
    window = settings["window_days"]
    errors = []
    if terminal is None:
        terminal = make_terminal(settings.get("terminal") or "cmux")

    progress("タブとプロセス")
    listed = terminal.list_tabs()
    if listed.get("error"):
        errors.append(listed["error"])
    procs, cwds = collect_processes()
    shell_cwd = tty_shell_cwd(procs, cwds)
    aliases = targets.get("path_aliases") or []
    live = collect_live_sessions(procs, aliases)

    progress("トランスクリプト")
    sessions = transcripts.collect(repo_paths, window, now, aliases)
    known_live = {}
    for s in sessions:
        lv = live.get(s["session_id"])
        s["live"] = lv
        if lv:
            known_live[s["session_id"]] = lv

    progress("git")
    repos = collect_git(repo_paths, window, now)
    for r in repos.values():
        if r.get("error"):
            errors.append(r["error"])

    progress("GitHub PR")
    owners = targets.get("owners") or {}
    prs = gh.collect(owners, list(repo_paths), pr_refs(sessions, owners), window, now,
                     settings.get("gh_cache_seconds", 300), force_gh)
    errors.extend(gh.errors)

    tabs = link_tabs(listed.get("tabs") or [], known_live, shell_cwd,
                     bool(terminal.capabilities.get("needs_input")))

    return {
        "now": now,
        "repos": repos,
        "sessions": sessions,
        "prs": prs,
        "tabs": tabs,
        "errors": errors,
        "terminal_error": listed.get("error"),
    }
