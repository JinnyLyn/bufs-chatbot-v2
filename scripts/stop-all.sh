#!/usr/bin/env bash
# stop-all.sh — Linux/H100 equivalent of scripts/stop-all.ps1.
#
# Stops OUR stack processes and nothing else. This box is SHARED: we never kill by
# port. Two ways a process qualifies:
#   1) its pid was recorded by start-all.sh in logs/run/*.pid AND its command line
#      still looks like the service (PIDs get recycled after reboots), or
#   2) no/stale pidfile: the process is identified by OWNER + IDENTITY — owned by this
#      user and tied to THIS repo checkout (see find_service_pids in _common.sh).
# Fallback (2) is what fixes the deploy trap where a service started by systemd or an
# old shell had no pidfile, survived stop-all, and start-all then said "already up" —
# leaving OLD code serving after a merge.
#
#   ./scripts/stop-all.sh              # frontend + backend (leaves Ollama up)
#   ./scripts/stop-all.sh --with-ollama
#
# Ollama is left running by default — the model stays resident in VRAM (LLM_KEEP_ALIVE=-1),
# and other users on the box may be sharing that server. Only the team-owned instance
# (the one on project/.env's OLLAMA_BASE_URL port) is ever touched, and only with
# --with-ollama.

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
derive_ollama_port

targets=(frontend backend)
case "${1:-}" in
    "") ;;
    --with-ollama)
        if [ "$OLLAMA_LOCAL" = 1 ]; then targets+=(ollama);
        else echo "ollama: remote per project/.env — not managed here"; fi ;;
    *)
        # An unrecognized flag must NOT fall through to a full stop — this script
        # takes services down.
        echo "usage: $0 [--with-ollama]" >&2; exit 2 ;;
esac

# What the recorded PID's command line must contain, per service (see start-all.sh):
#   frontend → node server.js (standalone, renames argv to "next-server") or npm run dev;
#   backend → python project/server.py.
expected_cmd() {
    case "$1" in
        frontend) echo 'node|npm|next' ;;
        backend)  echo 'server\.py|python' ;;
        ollama)   echo 'ollama' ;;
    esac
}

service_port() {
    case "$1" in
        frontend) echo "$FRONTEND_PORT" ;;
        backend)  echo "$BACKEND_PORT" ;;
        ollama)   echo "$OLLAMA_PORT" ;;
    esac
}

# TERM the process group (setsid gave each service its own; `npm run dev` forks
# children), wait up to 10s, then KILL. Never group-kill our own group.
kill_pid() {
    local name="$1" pid="$2" pgid own_pgid
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    own_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ] && [ "$pgid" != "$own_pgid" ]; then
        kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
        pgid=""
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "$name: pid $pid did not exit on TERM, sending KILL"
        [ -n "$pgid" ] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "$name: stopped (pid $pid)"
}

for name in "${targets[@]}"; do
    pidfile="$RUN_DIR/$name.pid"
    stopped_any=0

    # --- path 1: recorded pidfile, identity-checked ---
    if [ -f "$pidfile" ]; then
        pid="$(cat "$pidfile")"
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            echo "$name: pidfile stale (pid $pid not running)"
            rm -f "$pidfile"
        else
            args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
            if ! grep -qE "$(expected_cmd "$name")" <<<"$args"; then
                echo "$name: pid $pid is now '${args:-?}' — not ours (recycled PID), dropping pidfile without killing."
                rm -f "$pidfile"
            else
                kill_pid "$name" "$pid"
                rm -f "$pidfile"
                stopped_any=1
            fi
        fi
    fi

    # --- path 2: identity-based fallback for untracked survivors ---
    # (also runs after path 1 — `npm run dev` children can outlive a group kill edge case)
    for pid in $(find_service_pids "$name"); do
        kill -0 "$pid" 2>/dev/null || continue
        echo "$name: found untracked $name process (pid $pid) belonging to this repo"
        kill_pid "$name" "$pid"
        stopped_any=1
    done

    # --- verify the port actually freed — a busy port here means the next start-all
    #     would silently keep serving whatever is still bound to it.
    port="$(service_port "$name")"
    if [ -n "$port" ] && port_open "$port"; then
        echo "$name: WARNING — something is still listening on :$port (another user's process?)." >&2
        echo "$name: start-all.sh will treat it as 'already up'; investigate before restarting." >&2
    elif [ "$stopped_any" = 0 ]; then
        echo "$name: nothing to stop"
    fi
done

if [ "${1:-}" != "--with-ollama" ]; then
    echo "Note: Ollama left running (model stays warm in VRAM). Use --with-ollama to stop it too."
fi
