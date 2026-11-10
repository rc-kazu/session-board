"""orca (stablyai/orca): `orca terminal list|switch|close ... --json`。

実機では試していない。ドキュメントの出力 {terminals: [{handle, worktreeId, worktreePath, branch, title,
connected, exitCause?, ...}], totalCount, truncated} を前提にする。
全 worktree の一覧の取り方は版によって違う可能性があるので、次の順に試す:
  1. orca terminal list --worktree all --json
  2. orca worktree list --json で worktree を列挙し、それぞれ orca terminal list --worktree <id> --json
  3. orca terminal list --json (orca の既定の範囲だけ)
"""

import json
import os
import re

from .base import Terminal, command_error, tab

_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,127}$")
UNVERIFIABLE = "unverifiable"


def _load(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("result"), (dict, list)) and "terminals" not in data:
        data = data["result"]
    return data


def parse_list(text):
    """-> (tabs | None, truncated)。終了した端末 (exitCause あり / connected が false) は除く。"""
    data = _load(text)
    if not isinstance(data, dict) or not isinstance(data.get("terminals"), list):
        return None, False
    tabs = []
    for t in data["terminals"]:
        if not isinstance(t, dict):
            continue
        handle = t.get("handle")
        if not isinstance(handle, str) or not _ID_RE.match(handle):
            continue
        if t.get("exitCause") or t.get("connected") is False:
            continue
        wt = t.get("worktreePath") if isinstance(t.get("worktreePath"), str) else None
        branch = t.get("branch") if isinstance(t.get("branch"), str) else None
        group = branch or (os.path.basename(wt.rstrip("/")) if wt else "")
        tabs.append(tab(handle, group=group, title=t.get("title") or "", cwd=wt, worktree_path=wt, branch=branch,
                        worktree_id=t.get("worktreeId"), last_output_at=t.get("lastOutputAt")))
    return tabs, bool(data.get("truncated"))


def parse_worktrees(text):
    """orca worktree list --json -> worktree の ID の一覧。解釈できなければ None。"""
    data = _load(text)
    if isinstance(data, dict):
        data = data.get("worktrees")
    if not isinstance(data, list):
        return None
    out = []
    for w in data:
        if isinstance(w, dict):
            wid = w.get("id") or w.get("worktreeId")
            if isinstance(wid, str) and _ID_RE.match(wid):
                out.append(wid)
    return out


def parse_action_result(text):
    """switch / close の結果の status ("" は status なし)。JSON でなければ None。"""
    data = _load(text)
    if not isinstance(data, dict):
        return None
    st = data.get("status")
    return st if isinstance(st, str) else ""


class OrcaTerminal(Terminal):
    name = "orca"
    label = "orca"
    capabilities = {"focus": True, "close": True, "needs_input": False}

    def valid_id(self, tab_id):
        return isinstance(tab_id, str) and bool(_ID_RE.match(tab_id))

    def _list(self, extra):
        rc, out, _ = self._exec(["orca", "terminal", "list"] + extra + ["--json"])
        if rc != 0:
            return None, False
        return parse_list(out)

    def list_tabs(self):
        tabs, truncated = self._list(["--worktree", "all"])
        if tabs is None:
            rc, out, _ = self._exec(["orca", "worktree", "list", "--json"])
            ids = parse_worktrees(out) if rc == 0 else None
            if ids:
                tabs, seen = [], set()
                for wid in ids:
                    part, tr = self._list(["--worktree", wid])
                    truncated = truncated or tr
                    for t in part or []:
                        if t["id"] not in seen:
                            seen.add(t["id"])
                            tabs.append(t)
        if tabs is None:
            tabs, truncated = self._list([])
        if tabs is None:
            return {"tabs": [], "error": "orca からタブの一覧を取れませんでした (orca terminal list)"}
        err = "orca のタブの一覧は一部だけです (truncated)" if truncated else None
        return {"tabs": tabs, "error": err}

    def _id(self, t):
        if not self.valid_id(t.get("id")):
            raise ValueError("不正なタブです")
        return t["id"]

    def focus(self, t):
        cmd = ["orca", "terminal", "switch", "--terminal", self._id(t), "--json"]
        rc, out, err = self._exec(cmd)
        if rc != 0:
            return [command_error(cmd, rc, out, err)]
        return []

    def close(self, t):
        cmd = ["orca", "terminal", "close", "--terminal", self._id(t), "--json"]
        rc, out, err = self._exec(cmd)
        if rc != 0:
            return {"ok": False, "warning": None, "error": command_error(cmd, rc, out, err)}
        if parse_action_result(out) == UNVERIFIABLE:
            return {"ok": False, "warning": "orca が閉じたことを確認できませんでした (unverifiable)。orca で確かめてください",
                    "error": None}
        return {"ok": True, "warning": None, "error": None}
