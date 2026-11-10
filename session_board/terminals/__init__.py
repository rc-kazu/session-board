"""ターミナルのアダプタ。settings.json の "terminal" で選ぶ (既定は cmux)。

どのアダプタも同じ形のタブを返す (base.tab):
    {id, group, workspace_title, title, tty, cwd, worktree_path, branch, pid, needs_input}
needs_input はターミナル自身が「入力待ち」を知らせるときだけ bool。分からなければ None
(そのときは ~/.claude/sessions/<pid>.json の status で補う。collectors.link_tabs)。
"""

from .base import TERMINALS, Terminal, default_runner
from .cmux import CmuxTerminal
from .orca import OrcaTerminal
from .wezterm import WeztermTerminal

_CLASSES = {"cmux": CmuxTerminal, "wezterm": WeztermTerminal, "orca": OrcaTerminal, "none": Terminal}


def make_terminal(name, runner=None):
    cls = _CLASSES.get(name)
    if cls is None:
        raise ValueError("知らないターミナルです: %r (%s のどれか)" % (name, " / ".join(TERMINALS)))
    return cls(runner=runner)


__all__ = ["TERMINALS", "Terminal", "default_runner", "make_terminal"]
