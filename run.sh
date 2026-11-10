#!/bin/sh
# session-board を起動する。引数はそのまま渡す (例: ./run.sh --port 8766 --no-llm)
cd "$(dirname "$0")" || exit 1
exec python3 -m session_board "$@"
