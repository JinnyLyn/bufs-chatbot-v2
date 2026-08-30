#!/usr/bin/env bash
# stop-all.sh — Linux/H100 equivalent of scripts/stop-all.ps1.
#
# Stops only the processes start-all.sh recorded in logs/run/*.pid. This box is SHARED:
# killing by port would risk taking down another user's service, so we never do that.
# Each recorded PID is also verified against the expected command before any signal is
# sent — after a reboot PIDs get recycled, and a stale pidfile must never kill an
# unrelated process.
#
#   ./scripts/stop-all.sh              # frontend + backend (leaves Ollama up)
#   ./scripts/stop-all.sh --with-ollama
#
# Ollama is left running by default — the model stays resident in VRAM (LLM_KEEP_ALIVE=-1),
# and other users on the box may be sharing that server.

set -Eeuo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$REPO/logs/run"

targets=(frontend backend)
if [ "${1:-}" = "--with-ollama" ]; then targets+=(ollama); fi

# What the recorded PID's command line must contain, per service (see start-all.sh):
#   frontend → node server.js (standalone) or npm run dev; backend → python project/server.py.
expected_cmd() {
    case "$1" in
        frontend) echo 'node|npm|next' ;;
        backend)  echo 'server\.py|python' ;;
        ollama)   echo 'ollama' ;;
    esac
}

for name in "${targets[@]}"; do
    pidfile="$RUN_DIR/$name.pid"
    if [ ! -f "$pidfile" ]; then
        echo "$name: no pidfile (not started by start-all.sh?)"
        continue
    fi
    pid="$(cat "$pidfile")"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "$name: not running (stale pid $pid)"
        rm -f "$pidfile"
        continue
    fi
    # Identity check: PIDs are recycled — refuse to signal a process that doesn't look
    # like the service we started.
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    if ! grep -qE "$(expected_cmd "$name")" <<<"$args"; then
        echo "$name: pid $pid is now '${args:-?}' — not ours (recycled PID), dropping pidfile without killing."
        rm -f "$pidfile"
        continue
    fi

    # Terminate the whole process group — `npm run dev` and `ollama serve` both fork
    # children, and start-all.sh gave each service its own group via setsid.
    # Guard: never group-kill our own group (possible if start-all.sh ran without setsid).
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
        echo "$name: did not exit on TERM, sending KILL"
        [ -n "$pgid" ] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "$name: stopped (pid $pid)"
    rm -f "$pidfile"
done

if [ "${1:-}" != "--with-ollama" ]; then
    echo "Note: Ollama left running (model stays warm in VRAM). Use --with-ollama to stop it too."
fi
