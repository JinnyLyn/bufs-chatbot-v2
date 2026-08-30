#!/usr/bin/env bash
# stop-all.sh — Linux/H100 equivalent of scripts/stop-all.ps1.
#
# Stops only the processes start-all.sh recorded in logs/run/*.pid. This box is SHARED:
# killing by port would risk taking down another user's service, so we never do that.
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
    # Terminate the whole process group — `npm run dev` and `ollama serve` both fork children.
    # Guard: never group-kill our own group (possible if start-all.sh was `source`d).
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
