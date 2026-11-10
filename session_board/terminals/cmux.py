"""cmux (https://cmux.dev): `cmux tree --all --json` と `cmux sidebar-state` でタブと「Needs input」を取る。

cmux のソケットは cmux 配下のプロセスからの操作だけを受け付けるので、移動・閉じるは cmux のタブ内から
起動したボードでだけ動く。
"""

import concurrent.futures
import json
import re
import time

from .base import Terminal, command_error, tab

_ID_RE = re.compile(r"^(workspace:\d+)/(surface:\d+)$")
_WINDOW_RE = re.compile(r"^window:\d+$")
_PANE_RE = re.compile(r"^pane:\d+$")

# 前に出したあと、目的のタブにフォーカスが来ているかを確かめて選び直す回数と間隔
# (前に出した cmux があとから前のタブを選び直すことがあるので、合っていても少し待ってもう一度確かめる)
FOCUS_ATTEMPTS = 3
FOCUS_RETRY_DELAY = 0.3


def parse_sidebar_state(text):
    out = {"statuses": {}}
    for line in (text or "").splitlines():
        if line.startswith("  ") and "=" in line:
            k, v = line.strip().split("=", 1)
            out["statuses"][k] = v.split(" icon=")[0].split(" color=")[0].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def flatten_cmux_tree(tree):
    tabs = []
    for w in (tree or {}).get("windows") or []:
        for ws in w.get("workspaces") or []:
            panes = ws.get("panes") or []
            for pane_no, pane in enumerate(panes, 1):
                surfaces = pane.get("surfaces") or []
                for tab_no, s in enumerate(surfaces, 1):
                    if s.get("type") != "terminal":
                        continue
                    tabs.append({
                        "window_ref": w.get("ref"),
                        "workspace_ref": ws.get("ref"),
                        "workspace_title": ws.get("title") or "",
                        "pane_ref": pane.get("ref"),
                        "surface_ref": s.get("ref"),
                        "title": s.get("title") or "",
                        "tty": s.get("tty"),
                        # 同じパスのタブを見分けるための位置 (1 から。ブラウザのタブも数える)
                        "pane_index": pane_no, "pane_count": len(panes),
                        "tab_index": tab_no, "tab_count": len(surfaces),
                    })
    return tabs


def workspace_needs_input(sidebar):
    cc = ((sidebar or {}).get("statuses") or {}).get("claude_code", "")
    return cc.lower().startswith("needs input")


class CmuxTerminal(Terminal):
    name = "cmux"
    label = "cmux"
    capabilities = {"focus": True, "close": True, "needs_input": True}

    def valid_id(self, tab_id):
        return isinstance(tab_id, str) and bool(_ID_RE.match(tab_id))

    def list_tabs(self):
        rc, out, _ = self._exec(["cmux", "tree", "--all", "--json"])
        if rc != 0 or not out:
            return {"tabs": [], "error": "cmux からタブの一覧を取れませんでした (cmux tree)"}
        try:
            tree = json.loads(out)
        except ValueError:
            return {"tabs": [], "error": "cmux tree の出力を解釈できませんでした"}
        raw = [t for t in flatten_cmux_tree(tree) if t.get("workspace_ref") and t.get("surface_ref")]
        refs = sorted({t["workspace_ref"] for t in raw})

        def side(ref):
            rc2, text, _ = self._exec(["cmux", "sidebar-state", "--workspace", ref])
            return ref, parse_sidebar_state(text if rc2 == 0 else "")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            sidebars = dict(ex.map(side, refs))
        tabs = []
        for t in raw:
            extra = {k: t[k] for k in ("window_ref", "workspace_ref", "pane_ref", "surface_ref",
                                       "pane_index", "pane_count", "tab_index", "tab_count")}
            tabs.append(tab("%s/%s" % (t["workspace_ref"], t["surface_ref"]), group=t["workspace_title"],
                            title=t["title"], tty=t.get("tty"),
                            needs_input=workspace_needs_input(sidebars.get(t["workspace_ref"])), **extra))
        return {"tabs": tabs, "error": None}

    @staticmethod
    def _refs(t):
        m = _ID_RE.match(t.get("id") or "")
        if not m:
            raise ValueError("不正なタブです")
        return m.group(1), m.group(2)

    def __init__(self, runner=None, sleep=None):
        super().__init__(runner)
        self.sleep = sleep or time.sleep

    def focus(self, t):
        """ウィンドウ → ワークスペース → ペイン → タブ (surface) の順に選ぶ。

        アプリを前に出すと cmux はウィンドウで最後にフォーカスのあったタブを選び直すので、前に出すのは最初に行う。
        そのあと `cmux identify` で目的のタブにフォーカスがあるかを確かめ、違えば選び直す。
        """
        ws, sf = self._refs(t)
        win = t.get("window_ref") if _WINDOW_RE.match(t.get("window_ref") or "") else None
        pane = t.get("pane_ref") if _PANE_RE.match(t.get("pane_ref") or "") else None
        ctx = ["--workspace", ws] + (["--window", win] if win else [])
        if win:
            self._exec(["cmux", "focus-window", "--window", win])
        self._exec(["open", "-a", "cmux"])
        cmds = [["cmux", "select-workspace", "--workspace", ws] + (["--window", win] if win else [])]
        if pane:
            cmds.append(["cmux", "focus-pane", "--pane", pane] + ctx)
        cmds.append(["cmux", "focus-panel", "--panel", sf] + ctx)
        errors = []
        for attempt in range(FOCUS_ATTEMPTS):
            if attempt:
                self.sleep(FOCUS_RETRY_DELAY)
            errors = []
            for cmd in cmds:
                rc, out, err = self._exec(cmd)
                if rc != 0:
                    errors.append(command_error(cmd, rc, out, err))
            focused = self._focused_surface()
            if errors or focused is None:
                return errors
            if focused == sf:
                self.sleep(FOCUS_RETRY_DELAY)
                focused = self._focused_surface()
                if focused in (sf, None):
                    return errors
        return ["cmux で %s にフォーカスを移せませんでした (いまは %s)" % (sf, focused)]

    def _focused_surface(self):
        """いまフォーカスのあるタブの surface ref。確かめられなければ None。"""
        rc, out, _ = self._exec(["cmux", "identify", "--no-caller"])
        if rc != 0:
            return None
        try:
            ref = ((json.loads(out) or {}).get("focused") or {}).get("surface_ref")
        except (ValueError, AttributeError):
            return None
        return ref if isinstance(ref, str) else None

    def close(self, t):
        ws, sf = self._refs(t)
        cmd = ["cmux", "close-surface", "--surface", sf, "--workspace", ws]
        rc, out, err = self._exec(cmd)
        if rc != 0:
            return {"ok": False, "warning": None, "error": command_error(cmd, rc, out, err)}
        return {"ok": True, "warning": None, "error": None}
