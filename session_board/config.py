"""設定の置き場所・読み込み・検証。

- 設定: ${SESSION_BOARD_CONFIG_DIR:-~/.config/session-board} の targets.json (必須) と settings.json (任意)
- データ: ${SESSION_BOARD_DATA_DIR:-~/.local/share/session-board} (要約のキャッシュ・手動の上書き・作業報告)
- コードには config/*.example.json だけを同梱する。各項目の説明は README の「設定」にある
"""

import json
import os
import re
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "static")
EXAMPLE_DIR = os.path.join(ROOT, "config")
VERSION_FILE = os.path.join(ROOT, "VERSION")

KINDS = ("app", "infra-tf", "infra-k8s")
LAYOUTS = ("auto", "wide", "normal")
DEFAULT_OTHER_LABEL = "その他"

DEFAULT_SETTINGS = {
    "host": "127.0.0.1",
    "port": 8765,
    "window_days": 21,
    "stale_days": 3,
    "facts_refresh_seconds": 60,
    "gh_cache_seconds": 300,
    "llm_enabled": True,
    "llm_on_startup": True,
    "llm_model": "sonnet",
    "llm_claude_path": "",
    "llm_concurrency": 3,
    "llm_timeout_seconds": 120,
    "llm_input_chars": 5000,
    "llm_resummarize_min_new_user_msgs": 3,
    "llm_resummarize_min_interval_minutes": 60,
    "report_input_chars": 12000,
    "report_model": "sonnet",
    "report_timeout_seconds": 300,
    "ask_model": "sonnet",
    "ask_timeout_seconds": 180,
    "digest_enabled": True,
    "digest_model": "sonnet",
    "digest_timeout_seconds": 300,
    "digest_input_chars": 30000,
    "digest_lint_rounds": 2,
    "yomiyasu_dir": "",
    "column_wide_min_items": 8,
    "terminal": "cmux",
    "editor": "cursor",
    "editor_args": [],
}
TERMINALS = ("cmux", "wezterm", "orca", "none")
# 1 以上でないと動かない・意味が無い整数の設定
_POSITIVE_INTS = {
    "port", "window_days", "stale_days", "facts_refresh_seconds", "llm_concurrency", "llm_timeout_seconds",
    "llm_input_chars", "report_input_chars", "report_timeout_seconds", "column_wide_min_items",
    "ask_timeout_seconds", "digest_timeout_seconds", "digest_input_chars",
}

LABEL_MAX = 60  # 列の表示名の長さの上限
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


class ConfigError(Exception):
    """設定の誤り。errors は利用者に見せる日本語のメッセージの一覧。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


# ---------------------------------------------------------------- 置き場所

def _expand(path, home):
    if path == "~" or path.startswith("~/"):
        path = os.path.join(home, path[2:])
    return os.path.abspath(path)


def _home(env):
    return env.get("HOME") or os.path.expanduser("~")


def config_dir(env=None):
    env = os.environ if env is None else env
    home = _home(env)
    return _expand(env.get("SESSION_BOARD_CONFIG_DIR") or os.path.join(home, ".config", "session-board"), home)


def data_dir(env=None):
    env = os.environ if env is None else env
    home = _home(env)
    return _expand(env.get("SESSION_BOARD_DATA_DIR") or os.path.join(home, ".local", "share", "session-board"), home)


# 名前を変える前 (cmux-board) の置き場所。新しい置き場所が無ければ、起動したときにそこへ移す
OLD_NAME = "cmux-board"


def migrate_old_dirs(env=None):
    """前の名前の設定とデータのディレクトリを、新しい置き場所へ移す。戻り値は移した (元, 先) の一覧。

    環境変数で置き場所を指定しているとき・新しい置き場所が既にあるとき・前の置き場所が無いときは何もしない。
    """
    env = os.environ if env is None else env
    home = _home(env)
    pairs = []
    if not env.get("SESSION_BOARD_CONFIG_DIR"):
        pairs.append((os.path.join(home, ".config", OLD_NAME), config_dir(env)))
    if not env.get("SESSION_BOARD_DATA_DIR"):
        pairs.append((os.path.join(home, ".local", "share", OLD_NAME), data_dir(env)))
    moved = []
    for old, new in pairs:
        if os.path.isdir(old) and not os.path.lexists(new):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.rename(old, new)
            moved.append((old, new))
    return moved


CONFIG_DIR = config_dir()
DATA_DIR = data_dir()


def version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _prefixed(path, errors):
    return [msg if msg.startswith(path) else "%s: %s" % (path, msg) for msg in errors]


# ---------------------------------------------------------------- targets.json

def _is_meta(key):
    """_comment などの注記用のキー (読み飛ばす)。"""
    return isinstance(key, str) and key.startswith(("_", "$"))


def _unknown(obj, allowed, where, warnings):
    for k in obj:
        if not _is_meta(k) and k not in allowed:
            warnings.append("%s: 知らない項目 \"%s\" は無視します" % (where, k))


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def empty_targets(message=None):
    """targets.json が無いときの空のボード。"""
    return {"repos": {}, "owners": {}, "repo_kinds": {}, "projects": [], "targets": [],
            "other_label": DEFAULT_OTHER_LABEL, "path_aliases": [], "configured": False, "message": message}


def _validate_repos(raw, default_owner, home, errors, warnings):
    repos, owners, repo_kinds = {}, {}, {}
    raw_repos = raw.get("repos")
    if not isinstance(raw_repos, dict) or not any(not _is_meta(k) for k in raw_repos):
        errors.append("repos: 追跡するリポジトリを {\"リポジトリ名\": {\"owner\": ..., \"path\": ...}} の形で "
                      "1 つ以上書いてください")
        return {}, {}, {}, set()
    names = set()
    for name, spec in raw_repos.items():
        if _is_meta(name):
            continue
        names.add(name)
        where = "repos.%s" % name
        if not _NAME_RE.match(name):
            errors.append("%s: リポジトリ名は GitHub のリポジトリ名 (英数字・- _ .) にしてください" % where)
            continue
        if isinstance(spec, str):
            spec = {"path": spec}
        if not isinstance(spec, dict):
            errors.append("%s: {\"owner\": ..., \"path\": ...} の形で書いてください" % where)
            continue
        _unknown(spec, {"owner", "path", "default_kind"}, where, warnings)
        path = spec.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append("%s.path: ローカルのクローンの場所を書いてください (例: \"~/src/%s\")" % (where, name))
        else:
            repos[name] = _expand(path.strip(), home)
        owner = spec.get("owner", default_owner)
        if owner is None:
            errors.append("%s.owner: GitHub の owner がありません (%s.owner か、全体の github_owner を書いてください)"
                          % (where, where))
        elif not (isinstance(owner, str) and _NAME_RE.match(owner)):
            errors.append("%s.owner: GitHub の owner (組織名かユーザー名) を英数字・- _ . で書いてください "
                          "(\"%s\" は使えません)" % (where, owner))
        else:
            owners[name] = owner
        kind = spec.get("default_kind")
        if kind is not None:
            if kind not in KINDS:
                errors.append("%s.default_kind: %s のどれかにしてください" % (where, " / ".join(KINDS)))
            else:
                repo_kinds[name] = kind
    return repos, owners, repo_kinds, names


def _validate_rules(raw_rules, where, repo_names, errors, warnings):
    rules = []
    if not isinstance(raw_rules, list):
        errors.append("%s.rules: 配列にしてください" % where)
        return rules
    for j, r in enumerate(raw_rules):
        rw = "%s.rules[%d]" % (where, j)
        if not isinstance(r, dict):
            errors.append("%s: {\"repo\": ..., \"kind\": ..., \"paths\": [...]} の形で書いてください" % rw)
            continue
        _unknown(r, {"repo", "kind", "paths"}, rw, warnings)
        ok = True
        if r.get("repo") not in repo_names:
            errors.append("%s.repo: \"%s\" は repos にありません" % (rw, r.get("repo")))
            ok = False
        if r.get("kind") not in KINDS:
            errors.append("%s.kind: %s のどれかにしてください" % (rw, " / ".join(KINDS)))
            ok = False
        if not _str_list(r.get("paths")):
            errors.append("%s.paths: リポジトリのルートからのパスの配列にしてください "
                          "(例: [\"services/api\"]。リポジトリ全体なら [\"\"])" % rw)
            ok = False
        if ok:
            rules.append({"repo": r["repo"], "kind": r["kind"], "paths": list(r["paths"])})
    return rules


def _validate_projects(raw, other, repo_names, errors, warnings):
    raw_projects = raw.get("projects", raw.get("targets", []))
    if not isinstance(raw_projects, list):
        errors.append("projects: 案件の列を [ {\"name\": ..., \"rules\": [...]}, ... ] の配列で書いてください")
        return []
    projects = []
    seen = set()
    for i, p in enumerate(raw_projects):
        where = "projects[%d]" % i
        if not isinstance(p, dict):
            errors.append("%s: {\"name\": ..., \"rules\": [...]} の形で書いてください" % where)
            continue
        _unknown(p, {"name", "label", "keywords", "rules", "color", "layout", "cloud", "gcp"}, where, warnings)
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("%s.name: 列に出す名前を書いてください" % where)
            continue
        name = name.strip()
        if name in seen:
            errors.append("%s.name: \"%s\" が重複しています" % (where, name))
        if name == other:
            errors.append("%s.name: \"%s\" は other_label (どれにも当たらない列) と同じ名前なので使えません"
                          % (where, name))
        seen.add(name)
        keywords = p.get("keywords", [name])
        if not _str_list(keywords):
            errors.append("%s.keywords: 文字列の配列にしてください" % where)
            keywords = [name]
        rules = _validate_rules(p.get("rules", []), where, repo_names, errors, warnings)
        color = p.get("color")
        if color is not None and not (isinstance(color, str) and _COLOR_RE.match(color)):
            errors.append("%s.color: \"#3b5bdb\" のような 16 進の色にしてください" % where)
            color = None
        layout = p.get("layout", "auto")
        if layout not in LAYOUTS:
            errors.append("%s.layout: %s のどれかにしてください" % (where, " / ".join(LAYOUTS)))
            layout = "auto"
        cloud = p.get("cloud", p.get("gcp"))
        if cloud is not None and not isinstance(cloud, str):
            errors.append("%s.cloud: 文字列にしてください" % where)
            cloud = None
        # label は画面に出す名前 (画面から変えられる)。name は列の目印なので変えない
        label = p.get("label")
        if label is not None and not isinstance(label, str):
            errors.append("%s.label: 文字列にしてください" % where)
            label = None
        projects.append({"name": name, "label": (label or "").strip()[:LABEL_MAX] or None, "keywords": list(keywords),
                         "rules": rules, "color": color, "layout": layout, "cloud": cloud or None})
    return projects


def set_project_label(cdir, name, label, now=None):
    """targets.json の projects[name] の label を書き換える (空なら label を消す)。

    書く前に元のファイルを targets.json.bak-YYYYmmdd-HHMMSS として残す。ほかの項目と並び順はそのまま。
    戻り値は書いた label (消したときは None)。列が無ければ KeyError。
    """
    if label is not None and not isinstance(label, str):
        raise ValueError("文字列で指定してください。")
    label = (label or "").strip()[:LABEL_MAX]
    path = os.path.join(cdir, "targets.json")
    raw = _read_json(path)
    projects = raw.get("projects", raw.get("targets")) if isinstance(raw, dict) else None
    hit = next((p for p in projects or [] if isinstance(p, dict) and str(p.get("name", "")).strip() == name), None)
    if hit is None:
        raise KeyError("「%s」という列はありません。表示を更新してください。" % str(name)[:40])
    if label:
        hit["label"] = label
    else:
        hit.pop("label", None)
    validate_targets(raw)  # 壊れた形では書かない
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    backup = "%s.bak-%s" % (path, stamp)
    n = 1
    while os.path.lexists(backup):
        n += 1
        backup = "%s.bak-%s-%d" % (path, stamp, n)
    shutil.copy2(path, backup)
    tmp = path + ".tmp-label"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return label or None


def _alias_path(value, home):
    """path_aliases の 1 つのパス。絶対パスか ~ で始まるパスだけを受け付け、末尾の / を除く。"""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if not (value.startswith("/") or value == "~" or value.startswith("~/")):
        return None
    return _expand(value, home).rstrip("/") or "/"


def _validate_path_aliases(raw, home, errors):
    """{古い接頭辞: 新しい接頭辞} -> [(古い, 新しい), ...] (古い方の長い順)。"""
    raw_aliases = raw.get("path_aliases")
    if raw_aliases is None:
        return []
    if not isinstance(raw_aliases, dict):
        errors.append("path_aliases: {\"古い場所\": \"新しい場所\"} の形で書いてください "
                      "(例: {\"~/old/src\": \"~/src\"})")
        return []
    out = {}
    for old_raw, new_raw in raw_aliases.items():
        if _is_meta(old_raw):
            continue
        where = "path_aliases.%s" % old_raw
        old = _alias_path(old_raw, home)
        new = _alias_path(new_raw, home)
        if old is None:
            errors.append("%s: 古い場所は / か ~/ で始まるパスにしてください" % where)
            continue
        if old == "/":
            errors.append("%s: / は全部のパスに当たるので使えません" % where)
            continue
        if new is None:
            errors.append("%s: 新しい場所を / か ~/ で始まるパスの文字列で書いてください" % where)
            continue
        if old == new:
            errors.append("%s: 古い場所と新しい場所が同じです" % where)
            continue
        if old in out:
            errors.append("%s: 古い場所 %s が重複しています" % (where, old))
            continue
        out[old] = new
    return sorted(out.items(), key=lambda kv: (-len(kv[0]), kv[0]))


def validate_targets(raw, home=None):
    """targets.json の中身を検証して内部の形にする。戻り値 (targets, warnings)。誤りがあれば ConfigError。

    内部の形: repos {名前: 絶対パス}、owners {名前: owner}、repo_kinds {名前: 既定の種別}、
    projects [{name, keywords, rules, color, layout, cloud}] (targets は同じリスト)、other_label、
    path_aliases [(古い接頭辞, 新しい接頭辞)] (古い方の長い順。aliases.alias_path で使う)。
    誤りは 1 つずつではなく、見つかったものをまとめて返す。
    """
    home = home or os.path.expanduser("~")
    errors = []
    warnings = []
    if not isinstance(raw, dict):
        raise ConfigError(["targets.json: 全体を JSON のオブジェクト ({ ... }) にしてください"])
    _unknown(raw, {"github_owner", "github_org", "repos", "projects", "targets", "other_label", "path_aliases"},
             "targets.json",
             warnings)

    default_owner = raw.get("github_owner", raw.get("github_org"))
    if default_owner is not None and not (isinstance(default_owner, str) and _NAME_RE.match(default_owner)):
        errors.append("github_owner: GitHub の owner (組織名かユーザー名) を英数字・- _ . で書いてください")
        default_owner = None

    other = raw.get("other_label", DEFAULT_OTHER_LABEL)
    if not isinstance(other, str) or not other.strip():
        errors.append("other_label: どの案件にも当たらないものの列名を空でない文字列で書いてください")
        other = DEFAULT_OTHER_LABEL
    other = other.strip()

    repos, owners, repo_kinds, repo_names = _validate_repos(raw, default_owner, home, errors, warnings)
    projects = _validate_projects(raw, other, repo_names, errors, warnings)
    path_aliases = _validate_path_aliases(raw, home, errors)
    if errors:
        raise ConfigError(errors)
    return {"repos": repos, "owners": owners, "repo_kinds": repo_kinds, "projects": projects, "targets": projects,
            "other_label": other, "path_aliases": path_aliases, "configured": True, "message": None}, warnings


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        raise ConfigError(["%s: JSON として読めません (%s)" % (path, e)])
    except OSError as e:
        raise ConfigError(["%s: 読めません (%s)" % (path, e.strerror or e)])


def missing_message(cdir):
    return ("設定がありません: %s を作ってください。"
            "`session-board init` (リポジトリから動かすときは `./run.sh init`) で例 (targets.example.json) を"
            "コピーできます。" % os.path.join(cdir, "targets.json"))


def load_targets(cdir=None, warnings=None):
    """targets.json を読む。無ければ空のボード (configured False と案内文) を返す。誤りは ConfigError。"""
    cdir = cdir or CONFIG_DIR
    path = os.path.join(cdir, "targets.json")
    if not os.path.exists(path):
        return empty_targets(missing_message(cdir))
    try:
        t, warns = validate_targets(_read_json(path))
    except ConfigError as e:
        raise ConfigError(_prefixed(path, e.errors))
    if warnings is not None:
        warnings.extend(_prefixed(path, warns))
    return t


# ---------------------------------------------------------------- settings.json

def validate_settings(raw, warnings=None):
    """settings.json の中身を既定値に重ねて検証する。型の誤りは ConfigError、知らない項目は警告。"""
    if not isinstance(raw, dict):
        raise ConfigError(["settings.json: 全体を JSON のオブジェクト ({ ... }) にしてください"])
    errors = []
    s = dict(DEFAULT_SETTINGS)
    for k, v in raw.items():
        if _is_meta(k):
            continue
        if k not in DEFAULT_SETTINGS:
            if warnings is not None:
                warnings.append("settings.json: 知らない項目 \"%s\" は無視します" % k)
            continue
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            if not isinstance(v, bool):
                errors.append("settings.json の %s: true か false にしてください" % k)
                continue
        elif isinstance(default, int):
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append("settings.json の %s: 整数にしてください (例: %d)" % (k, default))
                continue
            if k in _POSITIVE_INTS and v <= 0:
                errors.append("settings.json の %s: 1 以上にしてください" % k)
                continue
            if k == "port" and v > 65535:
                errors.append("settings.json の port: 1〜65535 にしてください")
                continue
        elif isinstance(default, str) and not isinstance(v, str):
            errors.append("settings.json の %s: 文字列にしてください" % k)
            continue
        elif isinstance(default, list):
            if not _str_list(v):
                errors.append("settings.json の %s: 文字列の配列にしてください (例: [\"-n\"])" % k)
                continue
            v = list(v)
        if k == "terminal" and v not in TERMINALS:
            errors.append("settings.json の terminal: %s のどれかにしてください" % " / ".join(TERMINALS))
            continue
        if k == "editor" and not v.strip():
            errors.append("settings.json の editor: エディタのコマンド名か絶対パスを書いてください (例: \"cursor\")")
            continue
        s[k] = v
    if errors:
        raise ConfigError(errors)
    # 外部公開はしない: ホストはループバックに固定する
    if s.get("host") not in ("127.0.0.1", "localhost"):
        s["host"] = "127.0.0.1"
    return s


def load_settings(cdir=None, warnings=None):
    """settings.json を読む (無ければ既定値)。"""
    cdir = cdir or CONFIG_DIR
    path = os.path.join(cdir, "settings.json")
    if not os.path.exists(path):
        return dict(DEFAULT_SETTINGS)
    try:
        return validate_settings(_read_json(path), warnings)
    except ConfigError as e:
        raise ConfigError(_prefixed(path, e.errors))


# ---------------------------------------------------------------- init

def init_config(cdir=None):
    """例のファイルを設定ディレクトリにコピーする。既にあるファイルは上書きしない。作ったファイルを返す。"""
    cdir = cdir or CONFIG_DIR
    os.makedirs(cdir, exist_ok=True)
    created = []
    for name in ("targets", "settings"):
        dst = os.path.join(cdir, name + ".json")
        if os.path.exists(dst):
            continue
        shutil.copyfile(os.path.join(EXAMPLE_DIR, name + ".example.json"), dst)
        created.append(dst)
    return created
