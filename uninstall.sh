#!/bin/sh
# install.sh が作った ~/.local/bin/session-board だけを消す。
# 設定 (~/.config/session-board/) とデータ (~/.local/share/session-board/) とこのフォルダは消さない。
set -eu

LAUNCHER="$HOME/.local/bin/session-board"
MARK="# session-board launcher (install.sh)"

if [ ! -e "$LAUNCHER" ]; then
  echo "$LAUNCHER はありません。"
  exit 0
fi
if ! grep -q -F "$MARK" "$LAUNCHER" 2>/dev/null; then
  echo "$LAUNCHER は install.sh が作ったものではないので、消しません。" >&2
  exit 1
fi
rm -f "$LAUNCHER"
echo "削除しました: $LAUNCHER"
echo "設定 (${SESSION_BOARD_CONFIG_DIR:-$HOME/.config/session-board}) とデータ (${SESSION_BOARD_DATA_DIR:-$HOME/.local/share/session-board}) は残しています。要らなければ手で消してください。"
