#!/bin/sh
# session-board を使えるようにする (macOS 向け。sudo は不要。何度実行してもよい)。
#
# - このフォルダ (git のチェックアウト) から動かす。アプリをほかの場所へコピーしない
# - 必要なコマンドを確かめる (python3 3.9 以上は必須。git / gh / claude / ターミナルの CLI は無ければ注意を出す)
# - 例の設定を ~/.config/session-board/ にコピーする (既にあるファイルは上書きしない)
# - ~/.local/bin/session-board (このフォルダの run.sh を呼ぶ小さなスクリプト) を作る
#
# 使い方: ./install.sh      取り除くとき: ./uninstall.sh
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/session-board"
# 名前を変える前 (cmux-board) に install.sh が作った起動スクリプト。あれば消す
OLD_LAUNCHER="$BIN_DIR/cmux-board"
if [ -f "$OLD_LAUNCHER" ] && grep -q -F "# cmux-board launcher (install.sh)" "$OLD_LAUNCHER" 2>/dev/null; then
  rm -f "$OLD_LAUNCHER"
  echo "前の名前の起動スクリプトを消しました: $OLD_LAUNCHER"
fi
MARK="# session-board launcher (install.sh)"
WARN=0

ok()   { echo "  OK    $*"; }
warn() { echo "  注意  $*"; WARN=$((WARN + 1)); }
fail() { echo "  失敗  $*" >&2; exit 1; }

echo "session-board をインストールします: $ROOT"
echo ""
echo "必要なコマンドを確かめます。"

# python3 (必須)
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 がありません。Python 3.9 以上を入れてから、やり直してください。"
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  fail "python3 が古いです ($(python3 -V 2>&1))。Python 3.9 以上を入れてから、やり直してください。"
fi
ok "$(python3 -V 2>&1)"

# git
if command -v git >/dev/null 2>&1; then
  ok "$(git --version)"
else
  warn "git がありません。ブランチと worktree を集められません。"
fi

# gh
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    ok "gh (ログイン済み)"
  else
    warn "gh にログインしていません。PR を集めるには gh auth login を実行してください。"
  fi
else
  warn "gh (GitHub CLI) がありません。PR を集められません。https://cli.github.com/ から入れてください。"
fi

# claude
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
  ok "claude"
else
  warn "claude (Claude Code) がありません。要約と作業報告を作れません。"
fi

# --- 設定 -------------------------------------------------------------------
echo ""
echo "設定を用意します。"
(cd "$ROOT" && python3 -m session_board init) | sed 's/^/  /'

CONFIG_DIR=${SESSION_BOARD_CONFIG_DIR:-$HOME/.config/session-board}
TERMINAL=$(python3 - "$CONFIG_DIR/settings.json" <<'PY' 2>/dev/null || echo cmux
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("terminal") or "cmux")
except Exception:
    print("cmux")
PY
)

# ターミナル (settings.json の terminal)
case "$TERMINAL" in
  cmux)
    if command -v cmux >/dev/null 2>&1; then ok "ターミナル: cmux"
    else warn "ターミナル cmux の CLI (cmux) が PATH にありません。cmux のタブの中で起動すると使えます。"; fi ;;
  wezterm)
    if command -v wezterm >/dev/null 2>&1; then ok "ターミナル: wezterm"
    else warn "ターミナル wezterm の CLI (wezterm) が PATH にありません。"; fi ;;
  orca)
    if command -v orca >/dev/null 2>&1; then ok "ターミナル: orca"
    else warn "ターミナル orca の CLI (orca) が PATH にありません。"; fi ;;
  none)
    ok "ターミナル: none (タブは扱いません)" ;;
  *)
    warn "settings.json の terminal が分かりません: $TERMINAL (cmux / wezterm / orca / none)" ;;
esac

# --- 起動用のスクリプト -----------------------------------------------------
echo ""
echo "起動用のスクリプトを作ります。"
if [ -e "$LAUNCHER" ] && ! grep -q -F "$MARK" "$LAUNCHER" 2>/dev/null; then
  fail "$LAUNCHER は install.sh が作ったものではありません。上書きしないので、名前を変えるか消してから、やり直してください。"
fi
mkdir -p "$BIN_DIR"
q_root=$(printf '%s' "$ROOT" | sed "s/'/'\\\\''/g")
tmp="$LAUNCHER.tmp.$$"
cat > "$tmp" <<EOF
#!/bin/sh
$MARK
# このフォルダの session-board を動かす。引数はそのまま渡す (start / init / check / version、--port など)
SESSION_BOARD_HOME='$q_root'
if [ ! -x "\$SESSION_BOARD_HOME/run.sh" ]; then
  echo "session-board: \$SESSION_BOARD_HOME/run.sh がありません。フォルダを移したときは、移した先で ./install.sh を実行してください。" >&2
  exit 1
fi
exec "\$SESSION_BOARD_HOME/run.sh" "\$@"
EOF
chmod 755 "$tmp"
mv -f "$tmp" "$LAUNCHER"
chmod 755 "$ROOT/run.sh" 2>/dev/null || true
ok "$LAUNCHER (→ $ROOT/run.sh)"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR が PATH にありません。~/.zshrc に export PATH=\"\$HOME/.local/bin:\$PATH\" を足してください。" ;;
esac

# --- 次にやること -----------------------------------------------------------
echo ""
if [ "$WARN" -gt 0 ]; then
  echo "インストールしました (注意 $WARN 件。上を確かめてください)。"
else
  echo "インストールしました。"
fi
cat <<EOF

次にやること:
  1. $CONFIG_DIR/targets.json を自分のリポジトリと列に書き換える
  2. session-board check で設定を確かめる
  3. 設定したターミナル ($TERMINAL) のタブの中で session-board を実行し、http://127.0.0.1:8765/ を開く

更新するときは、このフォルダで git pull する (install.sh のやり直しは不要)。
EOF
