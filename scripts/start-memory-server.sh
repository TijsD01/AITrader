#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." >/dev/null 2>&1 && pwd )"
cd "$DIR"

PORT="${MEMORY_PORT:-8420}"
HOST="${MEMORY_HOST:-0.0.0.0}"

echo "=================================================================="
echo " [*] Starting TencentDB Agent Memory Hub & Proxy"
echo " [*] Base Directory: $DIR"
echo " [*] Listening on:   http://$HOST:$PORT"
echo " [*] Health check:   http://$HOST:$PORT/health"
echo " [*] Cold Start:     http://$HOST:$PORT/v3/memory/cold-start"
echo " [*] Assets Catalog: http://$HOST:$PORT/v3/memory/assets"
echo "=================================================================="

# Use virtualenv python if available
if [ -f "$DIR/.venv/bin/python3" ]; then
    PYTHON_EXE="$DIR/.venv/bin/python3"
else
    PYTHON_EXE="python3"
fi

exec "$PYTHON_EXE" -m src.memory.server --host "$HOST" --port "$PORT" "$@"
