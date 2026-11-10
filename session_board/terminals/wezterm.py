"""WezTerm: `wezterm cli list --format json` / `activate-pane` / `kill-pane` (ペイン単位)。

実機では試していない (ドキュメントの出力の形で書いた)。tty_name は新しい版にだけある。
"""

import json
import re
from urllib.parse import unquote, urlsplit

from .base import Terminal, command_error, short_tty, tab

_ID_RE = re.compile(r"^\d{1,9}$")


def file_url_to_path(url):
    """'file://host/a/b' -> '/a/b'。file URL でもパスでもなければ None。"""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("/"):
        return url
    u = urlsplit(url)
    if u.scheme != "file" or not u.path:
        return None
    return unquote(u.path)


def parse_list(text):
    """wezterm cli list の JSON -> タブの一覧。解釈できなければ None。"""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    tabs = []
    for p in data:
        if not isinstance(p, dict):
            continue
        pane_id = p.get("pane_id")
        if not isinstance(pane_id, int) or isinstance(pane_id, bool) or pane_id < 0:
            continue
        tabs.append(tab(str(pane_id), group=p.get("workspace") or "", title=p.get("title") or "",
                        tty=short_tty(p.get("tty_name")), cwd=file_url_to_path(p.get("cwd")),
                        window_id=p.get("window_id"), tab_id=p.get("tab_id")))
    return tabs


class WeztermTerminal(Terminal):
    name = "wezterm"
    label = "WezTerm"
    capabilities = {"focus": True, "close": True, "needs_input": False}

    def valid_id(self, tab_id):
        return isinstance(tab_id, str) and bool(_ID_RE.match(tab_id))

    def list_tabs(self):
        cmd = ["wezterm", "cli", "list", "--format", "json"]
        rc, out, err = self._exec(cmd)
        tabs = parse_list(out) if rc == 0 else None
        if tabs is None:
            return {"tabs": [], "error": "WezTerm からタブの一覧を取れませんでした (%s)" % command_error(cmd, rc, out, err)}
        return {"tabs": tabs, "error": None}

    def _id(self, t):
        if not self.valid_id(t.get("id")):
            raise ValueError("不正なタブです")
        return t["id"]

    def focus(self, t):
        cmd = ["wezterm", "cli", "activate-pane", "--pane-id", self._id(t)]
        rc, out, err = self._exec(cmd)
        if rc != 0:
            return [command_error(cmd, rc, out, err)]
        self._exec(["open", "-a", "WezTerm"])
        return []

    def close(self, t):
        cmd = ["wezterm", "cli", "kill-pane", "--pane-id", self._id(t)]
        rc, out, err = self._exec(cmd)
        if rc != 0:
            return {"ok": False, "warning": None, "error": command_error(cmd, rc, out, err)}
        return {"ok": True, "warning": None, "error": None}
