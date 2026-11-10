"""アダプタの共通部分と「なし」(タブを扱わない)。"""

import subprocess

TERMINALS = ("cmux", "wezterm", "orca", "none")


def default_runner(cmd, timeout=10):
    """引数リストで実行する (シェルを介さない)。戻り値 (returncode, stdout, stderr)。起動できなければ returncode None。"""
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "%s: %d 秒で応答がありません" % (cmd[0], timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def tab(id, group="", title="", tty=None, cwd=None, worktree_path=None, branch=None, pid=None, needs_input=None,
        **extra):
    """正規化したタブ。workspace_title は group の旧名 (grouping のキーワード判定が使う)。"""
    d = {"id": id, "group": group or "", "workspace_title": group or "", "title": title or "", "tty": tty,
         "cwd": cwd, "worktree_path": worktree_path, "branch": branch, "pid": pid, "needs_input": needs_input}
    d.update(extra)
    return d


def short_tty(name):
    """'/dev/ttys004' -> 'ttys004' (ps の表記にそろえる)。"""
    if not isinstance(name, str) or not name:
        return None
    return name[len("/dev/"):] if name.startswith("/dev/") else name


def command_error(cmd, rc, out, err):
    msg = (err or out or "").strip()[:200]
    if rc is None:
        return "%s を実行できません: %s" % (cmd[0], msg or "見つかりません")
    return "%s: %s" % (" ".join(cmd[:3]), msg or "終了コード %s" % rc)


class Terminal:
    """「なし」: タブを一覧せず、移動も閉じることもしない。ほかのアダプタの基底。"""

    name = "none"
    label = "なし"
    capabilities = {"focus": False, "close": False, "needs_input": False}

    def __init__(self, runner=None):
        self.run = runner or default_runner

    def info(self):
        return {"name": self.name, "label": self.label, "capabilities": dict(self.capabilities)}

    def valid_id(self, tab_id):
        return False

    def list_tabs(self):
        """{"tabs": [tab...], "error": str|None}"""
        return {"tabs": [], "error": None}

    def focus(self, t):
        """タブを前面に出す。戻り値はエラーの一覧 (空なら成功)。"""
        return ["このターミナル (%s) ではタブへ移動できません" % self.label]

    def close(self, t):
        """タブを閉じる。{"ok": bool, "warning": str|None, "error": str|None}"""
        return {"ok": False, "warning": None, "error": "このターミナル (%s) ではタブを閉じられません" % self.label}

    def _exec(self, cmd, timeout=10):
        rc, out, err = self.run(cmd, timeout=timeout)
        return rc, out, err
