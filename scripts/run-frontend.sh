#!/usr/bin/env bash
# run-frontend.sh — ExecStart for camchat-frontend.service: the Next.js standalone server
# in the foreground on FRONTEND_PORT (default 3000). Stages .next/static and public/
# beside server.js first (the standalone bundle does not include them), so a deploy is
# `npm run build` followed by a restart of this unit.
set -Eeuo pipefail
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
FRONTEND="$REPO/frontend"
SA="$FRONTEND/.next/standalone"
if [ ! -f "$SA/server.js" ]; then
    echo "[frontend] no standalone build at $SA/server.js — run: cd frontend && npm run build" >&2
    exit 1
fi
command -v node >/dev/null 2>&1 || { echo "[frontend] 'node' not on PATH (scripts/env.local PATH?)" >&2; exit 1; }
stage_standalone
cd "$SA"
echo "[frontend] starting standalone server on 127.0.0.1:$FRONTEND_PORT (build $(cat "$FRONTEND/.next/BUILD_ID" 2>/dev/null || echo '?'))"
exec env PORT="$FRONTEND_PORT" HOSTNAME=127.0.0.1 node server.js
