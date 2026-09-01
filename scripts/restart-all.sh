#!/usr/bin/env bash
# restart-all.sh — one command to redeploy the stack after a merge.
#
#   git checkout main && git pull        # get the code you want to serve
#   ./scripts/restart-all.sh             # rebuild frontend if stale, bounce backend+frontend
#
# What it does, in order:
#   1. Deploy sanity: prints the commit being served; warns when the worktree is not
#      on main, is behind origin/main, or is dirty (serving untracked local edits).
#   2. Frontend rebuild when needed: `npm run build` runs BEFORE anything is stopped,
#      so the old stack keeps serving during the build and downtime stays at the
#      restart itself. "Needed" = any frontend source newer than .next/BUILD_ID
#      (force with --build, skip with --no-build).
#   3. stop-all.sh  — kills our tracked AND untracked stack processes (see stop-all.sh).
#   4. start-all.sh — brings everything back up and waits for /health.
#   5. Probes /health and /health/llm so "restarted" means "answers, and can reach
#      the LLM", not just "process exists".
#
# Flags: --with-ollama   also bounce the team-owned ollama (model reload = slow start)
#        --build         force the frontend rebuild
#        --no-build      skip the rebuild check entirely
# Env: same as start-all.sh (BACKEND_PORT / FRONTEND_PORT / OLLAMA_PORT / ...).

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${BACKEND_PORT:-8000}"

with_ollama=""; build=auto
for arg in "$@"; do
    case "$arg" in
        --with-ollama) with_ollama="--with-ollama" ;;
        --build)       build=yes ;;
        --no-build)    build=no ;;
        *) echo "usage: $0 [--with-ollama] [--build|--no-build]" >&2; exit 2 ;;
    esac
done

# --- 1) deploy sanity ------------------------------------------------------
branch="$(git -C "$REPO" branch --show-current 2>/dev/null || echo '?')"
head="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "[deploy] serving $head on branch '$branch'"
[ "$branch" = "main" ] || echo "[warn]   not on main — the tunnel will serve '$branch'."
behind="$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
[ "$behind" = 0 ] || echo "[warn]   $behind commit(s) behind origin/main (as last fetched) — 'git pull' first?"
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
    echo "[warn]   worktree dirty — serving code that is not committed."
fi

# --- 2) frontend rebuild if stale -----------------------------------------
FRONTEND="$REPO/frontend"
BUILD_ID="$FRONTEND/.next/BUILD_ID"
if [ "$build" = auto ]; then
    build=no
    if [ ! -f "$BUILD_ID" ]; then
        build=yes
    else
        stale="$(find "$FRONTEND/src" "$FRONTEND/public" \
                      "$FRONTEND/package.json" "$FRONTEND/package-lock.json" \
                      "$FRONTEND/next.config.ts" "$FRONTEND/tsconfig.json" \
                      -newer "$BUILD_ID" -print -quit 2>/dev/null || true)"
        [ -n "$stale" ] && { echo "[build]  frontend sources changed since last build (e.g. ${stale#"$FRONTEND/"})"; build=yes; }
    fi
fi
if [ "$build" = yes ]; then
    echo "[build]  npm run build (old stack keeps serving meanwhile) -> logs/frontend/build.log"
    (cd "$FRONTEND" && npm run build >"$LOG_DIR/frontend/build.log" 2>&1) \
        || { echo "[error] frontend build FAILED — stack left untouched. See logs/frontend/build.log"; exit 1; }
fi

# --- 3+4) bounce -----------------------------------------------------------
"$REPO/scripts/stop-all.sh" ${with_ollama:+"$with_ollama"}
echo
"$REPO/scripts/start-all.sh"

# --- 5) end-to-end probe ---------------------------------------------------
echo
if curl -fsS --max-time 10 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
    if curl -fsS --max-time 20 "http://127.0.0.1:$BACKEND_PORT/health/llm" >/dev/null 2>&1; then
        echo "[done]   backend healthy and LLM reachable — $head is live."
    else
        echo "[warn]   backend up but /health/llm failed — LLM unreachable (check ollama)." >&2
        exit 1
    fi
else
    echo "[error]  /health not answering after restart — check logs/backend/server.err" >&2
    exit 1
fi
