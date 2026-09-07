#!/usr/bin/env bash
# run-backend.sh — ExecStart for camchat-backend.service: the FastAPI server in the
# foreground, from the repo venv, on BACKEND_PORT (default 8000). Same environment rules
# as start-all.sh (scripts/env.local is sourced by _common.sh).
set -Eeuo pipefail
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
BACKEND_PORT="${BACKEND_PORT:-8000}"
resolve_python || exit 1
cd "$REPO"
echo "[backend] starting on :$BACKEND_PORT ($(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
exec env PORT="$BACKEND_PORT" "$PYTHON" project/server.py
