#!/usr/bin/env bash
# run-backend.sh — ExecStart for camchat-backend.service: the FastAPI server in the
# foreground, from the repo venv, on BACKEND_PORT (default 8000). Same environment rules
# as start-all.sh (scripts/env.local is sourced by _common.sh).
set -Eeuo pipefail
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
BACKEND_PORT="${BACKEND_PORT:-8000}"
if [ -z "${PYTHON:-}" ] && [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; fi
PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import fastapi' >/dev/null 2>&1; then
    echo "[backend] '$PYTHON' cannot import fastapi — expected the repo venv at $REPO/.venv" >&2
    exit 1
fi
cd "$REPO"
echo "[backend] starting on :$BACKEND_PORT ($(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
exec env PORT="$BACKEND_PORT" "$PYTHON" project/server.py
