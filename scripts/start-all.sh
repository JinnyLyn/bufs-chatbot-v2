#!/usr/bin/env bash
# start-all.sh — Linux/H100 equivalent of scripts/start-all.ps1.
#
# Starts the full stack: Ollama (:11434), FastAPI backend (:8000), Next.js frontend (:3000).
# Idempotent: anything already listening is left alone. Logs go under <repo>/logs.
#
# On the H100 the LLM is LOCAL, so there is no SSH tunnel to preserve (the Windows box
# tunnelled :11434 to this machine; here Ollama simply runs on :11434 directly).
#
# Ports are overridable because this is a SHARED box — another user may already hold
# the defaults:
#   BACKEND_PORT=8010 FRONTEND_PORT=3010 OLLAMA_PORT=11500 ./scripts/start-all.sh
# An explicit OLLAMA_PORT is also exported to the backend as OLLAMA_BASE_URL (it would
# otherwise keep using project/.env's URL and talk to the wrong Ollama). An explicit
# BACKEND_PORT needs the cloudflared ingress updated AND the frontend rebuilt with
# BACKEND_ORIGIN=http://localhost:<port> (the /api rewrite is baked in at build time).
#
# Env:
#   BACKEND_PORT   (default 8000)   also exported as PORT for project/server.py
#   FRONTEND_PORT  (default 3000)
#   OLLAMA_PORT    (default 11434)  if set explicitly, exported to the backend (see above)
#   START_OLLAMA   (default auto)   auto|yes|no — "auto" starts one only if the port is free
#   FRONTEND_MODE  (default auto)   auto|prod|dev — "auto" uses the standalone build if present
#   PYTHON         (default python3)

set -Eeuo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/logs"
RUN_DIR="$LOG_DIR/run"
mkdir -p "$LOG_DIR"/{ollama,backend,frontend} "$RUN_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
# Remember whether the caller set OLLAMA_PORT — only then do we override the backend's
# OLLAMA_BASE_URL (otherwise project/.env stays in control, e.g. a deliberate remote URL).
ollama_port_explicit="${OLLAMA_PORT:+1}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
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

# Listening check with no external tools and no root: try to connect.
port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- 3>&- && return 0
    return 1
}

wait_port() {
    local port="$1" timeout="${2:-60}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        port_open "$port" && return 0
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

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
# on a shared machine. When we DIDN'T start a service, drop any stale pidfile so a later
# stop-all.sh can't act on a recycled PID.
write_pid() { echo "$2" >"$RUN_DIR/$1.pid"; }
drop_pid()  { rm -f "$RUN_DIR/$1.pid"; }

# --- 0) Qdrant runs embedded (single-writer). A stale lock means an ingest or an old
#        backend still holds the DB; starting a second writer fails at load time.
if [ -e "$REPO/qdrant_db/.lock" ] && ! port_open "$BACKEND_PORT"; then
    echo "[warn]  qdrant_db/.lock exists but nothing is on :$BACKEND_PORT."
    echo "        If no ingest/reindex is running, remove it: rm $REPO/qdrant_db/.lock"
fi

# --- 1) Ollama (local H100 GPU) ---
if port_open "$OLLAMA_PORT"; then
    echo "[ok]    Ollama already on :$OLLAMA_PORT"
    drop_pid ollama
elif [ "$START_OLLAMA" = "no" ]; then
    echo "[skip]  Ollama not running on :$OLLAMA_PORT (START_OLLAMA=no)"
    drop_pid ollama
elif ! command -v ollama >/dev/null 2>&1; then
    echo "[warn]  'ollama' not on PATH and nothing listening on :$OLLAMA_PORT — backend will fail to reach the LLM."
    drop_pid ollama
else
    echo "[start] Ollama :$OLLAMA_PORT"
    OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" $SETSID nohup ollama serve \
        >"$LOG_DIR/ollama/ollama.out" 2>"$LOG_DIR/ollama/ollama.err" &
    write_pid ollama $!
    wait_port "$OLLAMA_PORT" 30 || echo "[warn]  Ollama did not come up in 30s — see logs/ollama/"
fi

# --- 2) Backend (FastAPI) ---
if port_open "$BACKEND_PORT"; then
    echo "[ok]    backend already on :$BACKEND_PORT"
    drop_pid backend
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
    drop_pid frontend
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

echo
echo "Ollama   :$OLLAMA_PORT   -> $(port_open "$OLLAMA_PORT" && echo up || echo down)"
echo "Backend  :$BACKEND_PORT  -> $([ "$backend_up" = 1 ] && echo up || echo down)   (health: http://127.0.0.1:$BACKEND_PORT/health)"
echo "Frontend :$FRONTEND_PORT -> $([ "$frontend_up" = 1 ] && echo up || echo down)   (open:   http://127.0.0.1:$FRONTEND_PORT)"

if [ "$backend_up" != 1 ] || [ "$frontend_up" != 1 ]; then
    echo "Some services are not ready — check $LOG_DIR/ for details." >&2
    exit 1
fi
