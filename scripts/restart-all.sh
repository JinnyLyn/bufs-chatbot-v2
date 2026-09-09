#!/usr/bin/env bash
# restart-all.sh — rebuild if needed, then bounce the stack. The front door for a
# release is scripts/deploy.sh (checks out a release tag, calls this, records what is
# live, rolls back on failure — see RELEASE.md); run this directly only to restart the
# code that is already checked out.
#
#   ./scripts/deploy.sh v0.2.0-beta      # normal release: tag -> checkout -> this script
#   ./scripts/restart-all.sh             # plain restart of the current checkout
#
# What it does, in order:
#   1. Deploy sanity: prints the commit being served. At a release tag that is all; on a
#      branch it warns when the worktree is not on main, is behind origin/main, or is
#      dirty (serving untracked local edits).
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
# Exit: 0 healthy; 3 frontend build failed (stack NOT touched); 4 backend answers but the
#       LLM probe failed (up, degraded); 1 backend not answering after the bounce.
#       deploy.sh: 3 → restore the checkout only, 4 → keep the deploy and warn, 1 → roll back.

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
tag="$(head_release_tag)"
if [ -n "$tag" ]; then
    # A release tag is exactly what deploy.sh checks out — not being on main is the point.
    echo "[deploy] serving release $tag ($head)"
else
    echo "[deploy] serving $head on branch '${branch:-detached}'"
    [ "$branch" = "main" ] || echo "[warn]   not on main and not a release tag — the tunnel will serve '${branch:-$head}'."
    behind="$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
    [ "$behind" = 0 ] || echo "[warn]   $behind commit(s) behind origin/main (as last fetched) — releases go out via ./scripts/deploy.sh <tag> (RELEASE.md)."
fi
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
        || { echo "[error] frontend build FAILED — stack left untouched. See logs/frontend/build.log"; exit 3; }
fi

# --- 3+4) bounce -----------------------------------------------------------
# The healthcheck timer must not "recover" the stack while we are deliberately bouncing it.
maint_on "restart-all"; trap maint_off EXIT
"$REPO/scripts/stop-all.sh" ${with_ollama:+"$with_ollama"}
echo
"$REPO/scripts/start-all.sh"
maint_off; trap - EXIT

# --- 5) end-to-end probe ---------------------------------------------------
echo
if curl -fsS --max-time 10 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
    if curl -fsS --max-time 20 "http://127.0.0.1:$BACKEND_PORT/health/llm" >/dev/null 2>&1; then
        echo "[done]   backend healthy and LLM reachable — $head is live."
    else
        echo "[warn]   backend up but /health/llm failed — LLM unreachable (check ollama)." >&2
        exit 4
    fi
else
    echo "[error]  /health not answering after restart — check logs/backend/server.err" >&2
    exit 1
fi
