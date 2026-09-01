# _common.sh — shared helpers for start-all.sh / stop-all.sh / restart-all.sh.
# Sourced, not executed. Expects bash.
#
# Provides:
#   REPO, LOG_DIR, RUN_DIR            repo-anchored paths (created on source)
#   port_open / wait_port             TCP listening probes (no root, no external tools)
#   derive_ollama_port                OLLAMA_PORT from project/.env's OLLAMA_BASE_URL
#   find_service_pids <name>          discover OUR stack processes even without pidfiles
#
# Also sources scripts/env.local if present — box-local, gitignored settings such as
# CUDA_VISIBLE_DEVICES (MIG slice UUID on a shared GPU box) or PATH additions for
# node/nvm when running from systemd. Never commit env.local.

_COMMON_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$_COMMON_DIR/.." && pwd)"
LOG_DIR="$REPO/logs"
RUN_DIR="$LOG_DIR/run"
mkdir -p "$LOG_DIR"/{ollama,backend,frontend} "$RUN_DIR"

# shellcheck disable=SC1091
[ -f "$_COMMON_DIR/env.local" ] && . "$_COMMON_DIR/env.local"

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

# OLLAMA_PORT resolution — single source of truth is project/.env's OLLAMA_BASE_URL,
# because that is what the backend actually dials. The old hardcoded default (11434)
# is the SYSTEM ollama on this box: owned by another user, outside our CUDA_VISIBLE_DEVICES
# GPU isolation. Checking/starting on 11434 made the scripts manage the wrong server.
#
# Sets: OLLAMA_PORT (may stay empty), OLLAMA_LOCAL=1|0 (0 = remote URL in .env — the
# scripts must not try to start/stop ollama at all). A pre-set OLLAMA_PORT wins, but a
# disagreement with .env is warned about: the scripts would manage one ollama while the
# backend dials another (split-brain that surfaces only as /health/llm failing).
derive_ollama_port() {
    OLLAMA_LOCAL=1
    local url="" env_port=""
    if [ -f "$REPO/project/.env" ]; then
        # `|| true`: no OLLAMA_BASE_URL line is a valid state — under pipefail a bare
        # grep miss would otherwise abort the whole sourcing script with no output.
        url="$(grep -E '^OLLAMA_BASE_URL=' "$REPO/project/.env" | tail -1 | cut -d= -f2- | tr -d ' \r' || true)"
    fi
    case "$url" in
        http://127.0.0.1:*|http://localhost:*)
            env_port="$(printf '%s' "${url##*:}" | tr -cd '0-9')" ;;
        "") ;;
        *)  OLLAMA_LOCAL=0 ;;  # remote ollama — hands off
    esac
    if [ -n "${OLLAMA_PORT:-}" ]; then
        OLLAMA_LOCAL=1  # an explicit port always means "manage a local ollama there"
        if [ -n "$env_port" ] && [ "$OLLAMA_PORT" != "$env_port" ]; then
            echo "[warn]  OLLAMA_PORT=$OLLAMA_PORT disagrees with project/.env OLLAMA_BASE_URL (:$env_port)." >&2
            echo "        Scripts manage :$OLLAMA_PORT, but the backend dials .env unless OLLAMA_PORT came from the command line." >&2
        fi
        return 0
    fi
    if [ "$OLLAMA_LOCAL" = 0 ]; then OLLAMA_PORT=""; return 0; fi
    OLLAMA_PORT="${env_port:-11434}"   # no .env port — historical default
    return 0
}

# Discover the pids of OUR services by identity, not by port. This box is SHARED:
# we never kill by port. A process qualifies only if it is owned by this user AND its
# command line / cwd tie it to THIS repo checkout:
#   backend  → python running project/server.py with cwd (or path) in this repo
#   frontend → node/npm/next process whose cwd is under <repo>/frontend
#              (the standalone server renames its argv to "next-server (vX)", so
#              cwd is the reliable signal; name alone is not)
#   ollama   → "ollama serve" whose OLLAMA_HOST env pins it to 127.0.0.1:$OLLAMA_PORT
# This is what lets stop-all.sh work even when a service was started outside
# start-all.sh (systemd, an old shell) and no pidfile exists.
find_service_pids() {
    local name="$1" pid cwd cmd
    for pid in $(pgrep -u "$(id -un)" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        cmd="$({ tr '\0' ' ' < "/proc/$pid/cmdline"; } 2>/dev/null || true)"
        [ -n "$cmd" ] || continue
        case "$name" in
            backend)
                # Must be an actual python interpreter running server.py — a bare path
                # match would also hit an editor/tail/grep the user has open on the file.
                case "$cmd" in
                    *python*"$REPO/project/server.py"*) echo "$pid" ;;
                    *python*project/server.py*) if [ "$cwd" = "$REPO" ]; then echo "$pid"; fi ;;
                esac ;;
            frontend)
                # Only real server invocations: the standalone server renames its argv
                # to "next-server (vX)", start-all launches `node server.js` / `npm run
                # dev`, and dev mode spawns `.../.bin/next dev`. Loose globs like
                # *node*/*next* would match an editor on next.config.ts or an LSP.
                case "$cwd" in
                    "$REPO/frontend"|"$REPO/frontend/"*)
                        case "$cmd" in
                            next-server*|*"node server.js"*|*"npm run dev"*|*"next dev"*|*"next start"*) echo "$pid" ;;
                        esac ;;
                esac ;;
            ollama)
                case "$cmd" in
                    *"ollama serve"*)
                        [ -n "${OLLAMA_PORT:-}" ] || continue
                        if { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null \
                            | grep -qx "OLLAMA_HOST=127.0.0.1:$OLLAMA_PORT"; then
                            echo "$pid"
                        fi ;;
                esac ;;
        esac
    done
    return 0   # a failed guard on the last pid must not become the function's status (set -e)
}
