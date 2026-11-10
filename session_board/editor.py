"""「エディタで開く」: 案件の作業場所 (worktree > リポジトリのクローン > セッションの cwd) をエディタで開く。

- 開く場所はサーバ側で決めて検証する (設定のリポジトリ・その worktree・ボードが知っているセッションの cwd だけ)
- git には触らない (checkout しない)。クローンのいまのブランチが案件のブランチと違うときは画面に注記する
- コマンドは引数リストで実行する (シェルを介さない)。CLI が PATH に無いときは macOS の `open -a <アプリ>` で開く
"""

import os
import shutil
import subprocess
import tempfile
import time

# CLI 名 -> macOS のアプリ名 (CLI が PATH に無いときの open -a 用) と画面の名前
KNOWN = {
    "cursor": ("Cursor", "Cursor"),
    "code": ("Visual Studio Code", "VS Code"),
    "code-insiders": ("Visual Studio Code - Insiders", "VS Code Insiders"),
    "zed": ("Zed", "Zed"),
    "subl": ("Sublime Text", "Sublime Text"),
    "windsurf": ("Windsurf", "Windsurf"),
}


def display_name(editor):
    base = os.path.basename(str(editor or "").rstrip("/"))
    known = KNOWN.get(base.lower())
    return known[1] if known else base


def resolve_editor(cmd, which=shutil.which):
    """コマンド名なら PATH から、絶対パスならそのまま (実行できるときだけ)。見つからなければ None。"""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    cmd = cmd.strip()
    if os.path.isabs(cmd):
        return cmd if os.path.isfile(cmd) and os.access(cmd, os.X_OK) else None
    if "/" in cmd:
        return None
    return which(cmd)


def editor_command(editor, args, target, resolved):
    """実行するコマンド (引数リスト)。CLI が無ければ open -a <アプリ>。それも決められなければ None。"""
    if resolved:
        return [resolved] + list(args or []) + [target]
    name = str(editor or "").strip()
    if not name or "/" in name:
        return None
    app = KNOWN.get(name.lower(), (name, name))[0]
    return ["open", "-a", app, target]


def _main_branch(repos_info, repo, path):
    for w in (repos_info.get(repo) or {}).get("worktrees") or []:
        if w.get("path") == path:
            return w.get("branch")
    return None


def open_targets(item, repo_paths, repos_info):
    """案件を開く場所の候補 [{path, kind (worktree/main/cwd), label, note}] (純粋関数)。"""
    out = []
    seen = set()

    def add(path, kind, label, note=None):
        if path and path not in seen:
            seen.add(path)
            out.append({"path": path, "kind": kind, "label": label, "note": note})

    def main_clone(repo, branch):
        main = repo_paths.get(repo)
        if not main:
            return
        cur = _main_branch(repos_info, repo, main)
        note = "いまのブランチは %s" % cur if cur and branch and cur != branch else None
        add(main, "main", repo, note)

    for b in item.get("branches") or []:
        repo, branch, wt = b.get("repo"), b.get("branch"), b.get("worktree")
        main = repo_paths.get(repo)
        if wt and main and wt != main:
            add(wt, "worktree", "%s:%s" % (repo, branch))
        else:
            main_clone(repo, branch)
    covered = {b.get("repo") for b in item.get("branches") or []}
    for p in item.get("prs") or []:
        if p.get("repo") not in covered:
            covered.add(p.get("repo"))
            main_clone(p.get("repo"), p.get("headRefName"))
    if not out:
        for s in item.get("sessions") or []:
            cwd = s.get("cwd")
            if cwd:
                add(cwd, "cwd", os.path.basename(cwd.rstrip("/")) or cwd)
                break
    return out


def allowed_dirs(repo_paths, repos_info, session_cwds):
    """開いてよい場所 (実体のパス): 設定のリポジトリ・その git worktree・ボードが知っているセッションの cwd。"""
    out = set()
    paths = list(repo_paths.values())
    for info in repos_info.values():
        paths += [w.get("path") for w in (info or {}).get("worktrees") or []]
    paths += list(session_cwds)
    for p in paths:
        if isinstance(p, str) and os.path.isabs(p):
            out.add(os.path.realpath(p))
    return out


def validate_target(path, allowed):
    """path が許可された既存のディレクトリなら実体のパスを返す。そうでなければ ValueError。"""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("開く場所が不正です")
    if os.path.islink(path.rstrip("/") or "/"):
        raise ValueError("シンボリックリンクは開きません: " + path)
    real = os.path.realpath(path)
    if real not in allowed:
        raise ValueError("ボードが知っている場所ではありません: " + path)
    if not os.path.isdir(real):
        raise ValueError("ディレクトリがありません: " + path)
    return real


def launch(cmd, wait=1.5):
    """エディタを起動する。すぐに失敗したら ValueError (標準エラーの先頭を添える)。"""
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 start_new_session=True)
        except OSError as e:
            raise ValueError("エディタを起動できません: %s (%s)" % (cmd[0], e.strerror or e))
        deadline = time.time() + wait
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if p.returncode not in (None, 0):
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()[:300]
            raise ValueError("エディタが失敗しました (%s, 終了コード %d): %s" % (cmd[0], p.returncode, msg or "出力なし"))
