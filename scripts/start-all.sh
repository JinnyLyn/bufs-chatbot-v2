#!/usr/bin/env bash
# start-all.sh — Linux/H100 equivalent of scripts/start-all.ps1.
#
# Starts the full stack: Ollama, FastAPI backend (:8000), Next.js frontend (:3000).
# Idempotent: anything already listening is left alone (its pid is adopted into
# logs/run/*.pid when it is identifiably ours, so stop-all.sh can manage it later).
# Logs go under <repo>/logs.
#
# The Ollama port is derived from project/.env's OLLAMA_BASE_URL — the URL the backend
# actually dials (on the H100 that is the team-owned :11500 instance, NOT the system
# ollama on :11434 which belongs to another user and runs outside our GPU isolation).
# A remote OLLAMA_BASE_URL in .env means the scripts do not manage ollama at all.
#
# Ports are overridable because this is a SHARED box — another user may already hold
# the defaults:
#   BACKEND_PORT=8010 FRONTEND_PORT=3010 OLLAMA_PORT=11500 ./scripts/start-all.sh
# An explicit OLLAMA_PORT is also exported to the backend as OLLAMA_BASE_URL (it would
# otherwise keep using project/.env's URL and talk to the wrong Ollama). An explicit
# BACKEND_PORT needs the cloudflared ingress updated AND the frontend rebuilt with
# BACKEND_ORIGIN=http://localhost:<port> (the /api rewrite is baked in at build time).
#
# Env (scripts/env.local, if present, is sourced first — box-local, gitignored):
#   BACKEND_PORT   (default 8000)   also exported as PORT for project/server.py
#   FRONTEND_PORT  (default 3000)
#   OLLAMA_PORT    (default: port of OLLAMA_BASE_URL in project/.env, else 11434)
#   START_OLLAMA   (default auto)   auto|yes|no — "auto" starts one only if the port is free
#   FRONTEND_MODE  (default auto)   auto|prod|dev — "auto" uses the standalone build if present
#   PYTHON         (default <repo>/.venv/bin/python, else python3)
#   CUDA_VISIBLE_DEVICES            required to auto-start ollama (shared GPU box —
#                                   set the MIG slice UUID in scripts/env.local)

set -Eeuo pipefail

# Remember whether the caller set OLLAMA_PORT — only then do we override the backend's
# OLLAMA_BASE_URL (otherwise project/.env stays in control, e.g. a deliberate remote URL).
# Checked BEFORE sourcing _common.sh, which may load env.local / derive a port.
ollama_port_explicit="${OLLAMA_PORT:+1}"

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
derive_ollama_port
START_OLLAMA="${START_OLLAMA:-auto}"
FRONTEND_MODE="${FRONTEND_MODE:-auto}"
# Prefer the repo's own venv over whatever `python3` happens to resolve to.
# Bare `python3` on this box resolves to miniconda, which has none of the app's
# dependencies — so starting the stack from a shell without the venv activated crashed
# the backend at import with "ModuleNotFoundError: No module named 'fastapi'", while a
# shell that happened to have it activated worked. That made the failure look
# intermittent and unrelated to the script. Resolve it here instead of relying on the
# caller's environment. An explicit PYTHON= still wins.
if [ -z "${PYTHON:-}" ] && [ -x "$REPO/.venv/bin/python" ]; then
    PYTHON="$REPO/.venv/bin/python"
fi
PYTHON="${PYTHON:-python3}"

# Fail loudly and early rather than launching an interpreter that cannot import the app.
if ! "$PYTHON" -c 'import fastapi' >/dev/null 2>&1; then
    echo "[error] '$PYTHON' cannot import fastapi — the backend would crash at startup."
    echo "        Expected the repo venv at $REPO/.venv (create it, or set PYTHON=...)."
    exit 1
fi

# Each service is launched under setsid so it gets its OWN process group/session:
# stop-all.sh group-kills per service, and without setsid all three would share this
# script's group (stopping one would take down the others — verified behavior).
SETSID="$(command -v setsid || true)"
[ -n "$SETSID" ] || echo "[warn]  setsid not found — stop-all.sh may not reap child processes cleanly."

# Poll a URL for 200; if a pidfile is given, bail out as soon as that process dies
# (a backend that crashes at boot should fail in seconds, not after the full timeout).
wait_http_200() {
    local url="$1" timeout="${2:-180}" pidfile="${3:-}" waited=0 pid=""
    [ -n "$pidfile" ] && [ -f "$pidfile" ] && pid="$(cat "$pidfile")"
    while [ "$waited" -lt "$timeout" ]; do
        if curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then return 0; fi
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            echo "[fail]  process $pid (from $(basename "$pidfile")) exited during startup." >&2
            return 1
        fi
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

# Record a PID so stop-all.sh can shut down exactly what we started — never kill by port
# on a shared machine.
write_pid() { echo "$2" >"$RUN_DIR/$1.pid"; }
drop_pid()  { rm -f "$RUN_DIR/$1.pid"; }

# When a service is already listening, it may still be OURS (started by systemd or an
# earlier shell whose pidfile is gone). Adopt its pid so stop-all.sh / restart-all.sh
# keep working; only drop the pidfile when nothing identifiable is found. Dropping is
# what previously made "stop-all then start-all" leave stale processes serving old code.
adopt_pid() {
    local name="$1" pids
    pids="$(find_service_pids "$name" | head -2 || true)"
    if [ "$(wc -l <<<"$pids")" = 1 ] && [ -n "$pids" ]; then
        write_pid "$name" "$pids"
        echo "        (adopted running $name pid $pids into $name.pid)"
    else
        drop_pid "$name"
    fi
}

# --- 0) Qdrant runs embedded (single-writer): a second writer fails at load time.
#        The lock FILE always survives its owner (the lock itself is an OS-level
#        advisory lock released when the process dies), so its mere presence is not a
#        problem — every restart tripped the old warning between stop and start. Warn
#        only when something actually holds it right now.
_qlock="$REPO/qdrant_db/.lock"
if [ -e "$_qlock" ] && ! port_open "$BACKEND_PORT"; then
    _holders="$(lock_holders "$_qlock")" || _holders="?"
    if [ "$_holders" = "?" ]; then
        echo "[warn]  qdrant_db/.lock exists but nothing is on :$BACKEND_PORT (holder unknown: no /proc)."
        echo "        If no ingest/reindex is running, remove it: rm $_qlock"
    elif [ -n "$_holders" ]; then
        echo "[warn]  qdrant_db is held by pid(s) $(echo "$_holders" | tr '\n' ' ')— an ingest/reindex"
        echo "        or an old backend still owns it. The backend will fail to load until that stops."
    fi
    # Nobody holds it: leftover file, harmless — Qdrant reuses it. Say nothing.
fi
unset _qlock _holders

# --- 1) Ollama (local H100 GPU) ---
if [ "$OLLAMA_LOCAL" != 1 ]; then
    echo "[skip]  OLLAMA_BASE_URL in project/.env is remote — not managing ollama."
elif port_open "$OLLAMA_PORT"; then
    echo "[ok]    Ollama already on :$OLLAMA_PORT"
    adopt_pid ollama
elif [ "$START_OLLAMA" = "no" ]; then
    echo "[skip]  Ollama not running on :$OLLAMA_PORT (START_OLLAMA=no)"
    drop_pid ollama
elif ! command -v ollama >/dev/null 2>&1; then
    echo "[warn]  'ollama' not on PATH and nothing listening on :$OLLAMA_PORT — backend will fail to reach the LLM."
    drop_pid ollama
elif [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
    # Shared GPU box: an ollama started without the MIG slice UUID grabs whatever GPU
    # it likes (or CPU-falls-back) and can poach another team's device. Refuse.
    echo "[error] refusing to start Ollama: CUDA_VISIBLE_DEVICES is not set."
    echo "        Put the MIG slice UUID in scripts/env.local, e.g.:"
    echo "        export CUDA_VISIBLE_DEVICES=MIG-xxxxxxxx-...."
    drop_pid ollama
else
    echo "[start] Ollama :$OLLAMA_PORT (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
    OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" $SETSID nohup ollama serve \
        >"$LOG_DIR/ollama/ollama.out" 2>"$LOG_DIR/ollama/ollama.err" &
    write_pid ollama $!
    wait_port "$OLLAMA_PORT" 30 || echo "[warn]  Ollama did not come up in 30s — see logs/ollama/"
fi

# --- 2) Backend (FastAPI) ---
if port_open "$BACKEND_PORT"; then
    echo "[ok]    backend already on :$BACKEND_PORT"
    adopt_pid backend
else
    echo "[start] backend :$BACKEND_PORT"
    if [ -n "$ollama_port_explicit" ]; then
        echo "        (OLLAMA_PORT set — overriding backend OLLAMA_BASE_URL=http://127.0.0.1:$OLLAMA_PORT)"
        export OLLAMA_BASE_URL="http://127.0.0.1:$OLLAMA_PORT"
    fi
    (
        cd "$REPO"
        PORT="$BACKEND_PORT" $SETSID nohup "$PYTHON" project/server.py \
            >"$LOG_DIR/backend/server.out" 2>"$LOG_DIR/backend/server.err" &
        write_pid backend $!
    )
fi

# --- 3) Frontend (Next.js) ---
# next.config.ts sets output:"standalone", so `next start` does NOT work — the standalone
# server must be run directly, with static assets staged beside it.
FRONTEND="$REPO/frontend"
STANDALONE="$FRONTEND/.next/standalone/server.js"

stage_standalone() {
    # The standalone bundle ships only server code; Next expects .next/static and public/
    # to be copied in alongside it. Remove the previous copies first — `cp -r src dst` nests
    # into an existing dst (static/static) instead of replacing it.
    local sa="$FRONTEND/.next/standalone"
    mkdir -p "$sa/.next"
    rm -rf "$sa/.next/static"
    cp -r "$FRONTEND/.next/static" "$sa/.next/static"
    if [ -d "$FRONTEND/public" ]; then
        rm -rf "$sa/public"
        cp -r "$FRONTEND/public" "$sa/public"
    fi
}

if port_open "$FRONTEND_PORT"; then
    echo "[ok]    frontend already on :$FRONTEND_PORT"
    adopt_pid frontend
else
    mode="$FRONTEND_MODE"
    if [ "$mode" = "auto" ]; then
        if [ -f "$STANDALONE" ]; then mode=prod; else mode=dev; fi
    fi

    if [ "$mode" = "prod" ]; then
        [ -f "$STANDALONE" ] || { echo "[build] npm run build (no standalone bundle yet)"; (cd "$FRONTEND" && npm run build >"$LOG_DIR/frontend/build.log" 2>&1); }
        stage_standalone
        echo "[start] frontend :$FRONTEND_PORT (standalone)"
        (
            cd "$FRONTEND/.next/standalone"
            PORT="$FRONTEND_PORT" HOSTNAME=127.0.0.1 $SETSID nohup node server.js \
                >"$LOG_DIR/frontend/frontend.out" 2>"$LOG_DIR/frontend/frontend.err" &
            write_pid frontend $!
        )
    else
        echo "[start] frontend :$FRONTEND_PORT (npm run dev)"
        (
            cd "$FRONTEND"
            PORT="$FRONTEND_PORT" $SETSID nohup npm run dev \
                >"$LOG_DIR/frontend/frontend.out" 2>"$LOG_DIR/frontend/frontend.err" &
            write_pid frontend $!
        )
    fi
fi

# --- readiness -------------------------------------------------------------
# /health only returns 200 after the embedding model + graph are built, so allow a while.
backend_up=0; wait_http_200 "http://127.0.0.1:$BACKEND_PORT/health" 300 "$RUN_DIR/backend.pid" && backend_up=1
frontend_up=0; wait_port "$FRONTEND_PORT" 90 && frontend_up=1
ollama_up=1
if [ "$OLLAMA_LOCAL" = 1 ] && [ "$START_OLLAMA" != "no" ]; then
    ollama_up=0; port_open "$OLLAMA_PORT" && ollama_up=1
fi

echo
if [ "$OLLAMA_LOCAL" = 1 ]; then
    echo "Ollama   :$OLLAMA_PORT   -> $(port_open "$OLLAMA_PORT" && echo up || echo down)"
else
    echo "Ollama   remote (project/.env OLLAMA_BASE_URL) — not managed here"
fi
echo "Backend  :$BACKEND_PORT  -> $([ "$backend_up" = 1 ] && echo up || echo down)   (health: http://127.0.0.1:$BACKEND_PORT/health)"
echo "Frontend :$FRONTEND_PORT -> $([ "$frontend_up" = 1 ] && echo up || echo down)   (open:   http://127.0.0.1:$FRONTEND_PORT)"

if [ "$backend_up" != 1 ] || [ "$frontend_up" != 1 ] || [ "$ollama_up" != 1 ]; then
    echo "Some services are not ready — check $LOG_DIR/ for details." >&2
    exit 1
fi
