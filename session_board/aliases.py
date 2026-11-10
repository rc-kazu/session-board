"""path_aliases: 作業ディレクトリを移した後も、記録に残る古いパスを新しいパスに読み替える (純粋関数)。

Claude Code のトランスクリプトやセッションの json には、記録した時点の cwd・編集したファイル・worktree の
パスがそのまま残る。~/src を ~/work に移したような場合、そのままでは追跡リポジトリの下と判定できない。
aliases は config.validate_targets が作る [(古い接頭辞, 新しい接頭辞), ...] (古い方の長い順、どちらも絶対パス)。
一致はパスの区切りの上だけ (/a/b は /a/b と /a/b/... に当たり、/a/bc には当たらない)。読み替えは 1 回だけ。
"""


def _under(path, root):
    return path == root or path.startswith(root + "/")


def alias_path(path, aliases):
    """path の先頭が古い接頭辞なら新しい接頭辞に置き換える。当たらなければそのまま返す。"""
    if not aliases or not isinstance(path, str) or not path.startswith("/"):
        return path
    for old, new in aliases:
        if _under(path, old):
            return new + path[len(old):]
    return path


def old_forms(path, aliases):
    """新しいパス path を、読み替える前の古いパスに戻したもの (projects のディレクトリ名を探すのに使う)。"""
    out = []
    for old, new in aliases or []:
        if _under(path, new):
            p = old + path[len(new):]
            if p not in out and alias_path(p, aliases) == path:
                out.append(p)
    return out


def alias_session(s, aliases):
    """transcript.parse_lines の結果のパスを読み替えた新しい dict を返す (元は変えない)。

    読み替えで同じになった locations ((cwd, ブランチ) ごと) は 1 つにまとめ、files_edited / edits の重複は除く。
    """
    if not aliases:
        return s
    out = dict(s)
    out["cwd_last"] = alias_path(s.get("cwd_last"), aliases)

    locations, index = [], {}
    for loc in s.get("locations") or []:
        cwd = alias_path(loc.get("cwd"), aliases)
        key = (cwd, loc.get("branch"))
        if key in index:
            cur = index[key]
            cur["count"] = cur.get("count", 0) + loc.get("count", 0)
            if (loc.get("last_ts") or "") > (cur.get("last_ts") or ""):
                cur["last_ts"] = loc.get("last_ts")
            continue
        cur = dict(loc, cwd=cwd)
        index[key] = cur
        locations.append(cur)
    out["locations"] = locations

    out["files_edited"] = list(dict.fromkeys(alias_path(f, aliases) for f in s.get("files_edited") or []))

    edits, seen = [], set()
    for e in s.get("edits") or []:
        file = alias_path(e.get("file"), aliases)
        if (file, e.get("branch")) in seen:
            continue
        seen.add((file, e.get("branch")))
        edits.append(dict(e, file=file, cwd=alias_path(e.get("cwd"), aliases)))
    out["edits"] = edits
    return out


def alias_day_session(s, aliases):
    """report.parse_day_lines の結果 (cwd、files) を読み替えた新しい dict を返す。"""
    if not aliases or not s:
        return s
    out = dict(s)
    out["cwd"] = alias_path(s.get("cwd"), aliases)
    out["files"] = list(dict.fromkeys(alias_path(f, aliases) for f in s.get("files") or []))
    return out


def alias_live(lv, aliases):
    """~/.claude/sessions/<pid>.json から作った稼働中のセッションの cwd を読み替える。"""
    if not aliases or not lv:
        return lv
    return dict(lv, cwd=alias_path(lv.get("cwd"), aliases))
