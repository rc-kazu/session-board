"""(repo, path) から対象 (target, kind) を判定する純粋ロジック。"""

import re

_WORKTREE_MARKERS = (
    ".claude/worktrees/",
    ".worktrees/",
)
# 削除済み worktree の名前がブランチ風に 2 階層 (feature/xxx) になっている場合の接頭辞。
# `.worktrees/.group/<name>` のように、名前の前に「.」で始まるまとめ用のディレクトリを挟んでいる場合も 2 階層として扱う。
_BRANCH_PREFIXES = {"feature", "feat", "fix", "chore", "hotfix", "infra", "deploy", "refactor", "docs"}


def glob_to_regex(pattern):
    """glob パターンを「パス接頭辞として一致」する正規表現に変換する。

    `*` は 1 セグメント内の任意文字列、`**` はスラッシュを含む任意文字列。
    空文字列はリポジトリ全体に一致する。
    """
    if pattern == "":
        return re.compile(r"^.*$", re.S)
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + r"(?:/.*)?$", re.S)


DEFAULT_OTHER_LABEL = "その他"


class Classifier:
    """targets.json (config.validate_targets の結果) の projects と rules で判定する。

    other_label はどの列にも当たらない案件の列名、repo_kinds はリポジトリの既定の種別 (PR の表示用)。
    """

    def __init__(self, targets_config):
        self.target_names = []
        self.keywords = []
        self.rules = []  # (repo, regex, target, kind)
        self.other_label = targets_config.get("other_label") or DEFAULT_OTHER_LABEL
        self.repo_kinds = dict(targets_config.get("repo_kinds") or {})
        projects = targets_config.get("projects")
        if projects is None:
            projects = targets_config.get("targets", [])
        for t in projects:
            name = t["name"]
            self.target_names.append(name)
            for kw in t.get("keywords", [name]):
                self.keywords.append((kw.lower(), name))
            for rule in t.get("rules", []):
                for p in rule.get("paths", []):
                    self.rules.append((rule["repo"], glob_to_regex(p.strip("/")), name, rule["kind"]))

    def classify(self, repo, rel):
        """(repo, リポジトリルートからの相対パス) -> [(target, kind), ...]"""
        rel = (rel or "").strip("/")
        found = []
        for r_repo, rx, target, kind in self.rules:
            if r_repo == repo and rx.match(rel):
                if (target, kind) not in found:
                    found.append((target, kind))
        return found

    def keyword_targets(self, text):
        """ブランチ名などに対象名が含まれていれば返す (パス判定できないときの補助)。"""
        low = (text or "").lower()
        out = []
        for kw, name in self.keywords:
            if kw and kw in low and name not in out:
                out.append(name)
        return out


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _rel(path, root):
    if path == root:
        return ""
    return path[len(root.rstrip("/")) + 1:]


def resolve_path(abs_path, repos, worktrees):
    """絶対パスをリポジトリ・worktree・相対パスに解決する。

    repos: {repo_name: clone_path}
    worktrees: {repo_name: [{"path":..., "branch":...}, ...]}  (git worktree list 由来)
    戻り値: {"repo", "rel", "worktree", "branch"} または None
    """
    if not abs_path:
        return None
    abs_path = abs_path.rstrip("/") or "/"
    best = None
    for repo, root in repos.items():
        if _under(abs_path, root):
            if best is None or len(root) > len(best[1]):
                best = (repo, root)
    if best is None:
        return None
    repo, root = best

    # 登録済み worktree のうち最長一致を採用
    wt_best = None
    for wt in worktrees.get(repo, []):
        if _under(abs_path, wt["path"]):
            if wt_best is None or len(wt["path"]) > len(wt_best["path"]):
                wt_best = wt
    if wt_best is not None and wt_best["path"].rstrip("/") != root.rstrip("/"):
        return {
            "repo": repo,
            "rel": _rel(abs_path, wt_best["path"]),
            "worktree": wt_best["path"],
            "branch": wt_best.get("branch"),
        }

    rel = _rel(abs_path, root)
    # 削除済み worktree の推定
    for marker in _WORKTREE_MARKERS:
        idx = rel.find(marker) if not rel.startswith(marker) else 0
        if idx == -1:
            continue
        if idx > 0 and rel[idx - 1] != "/":
            continue
        rest = rel[idx + len(marker):]
        if not rest:
            break
        parts = rest.split("/")
        take = 1
        if len(parts) > 2 and (parts[0] in _BRANCH_PREFIXES or parts[0].startswith(".")):
            take = 2
        name_parts = parts[:take]
        wt_path = root.rstrip("/") + "/" + rel[:idx] + marker + "/".join(name_parts)
        return {
            "repo": repo,
            "rel": "/".join(parts[take:]),
            "worktree": wt_path,
            "branch": None,
        }

    main_branch = None
    if wt_best is not None:
        main_branch = wt_best.get("branch")
    return {"repo": repo, "rel": rel, "worktree": root, "branch": main_branch}
