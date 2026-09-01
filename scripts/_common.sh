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
# scripts must not try to start/stop ollama at all).
derive_ollama_port() {
    OLLAMA_LOCAL=1
    if [ -n "${OLLAMA_PORT:-}" ]; then return 0; fi
    local url=""
    if [ -f "$REPO/project/.env" ]; then
        url="$(grep -E '^OLLAMA_BASE_URL=' "$REPO/project/.env" | tail -1 | cut -d= -f2- | tr -d ' \r')"
    fi
    case "$url" in
        http://127.0.0.1:*|http://localhost:*)
            OLLAMA_PORT="$(printf '%s' "${url##*:}" | tr -cd '0-9')" ;;
        "")
            OLLAMA_PORT=11434 ;;   # no .env — historical default
        *)
            OLLAMA_LOCAL=0; OLLAMA_PORT="" ;;  # remote ollama — hands off
    esac
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
                case "$cmd" in
                    *"$REPO/project/server.py"*) echo "$pid" ;;
                    *python*project/server.py*) [ "$cwd" = "$REPO" ] && echo "$pid" ;;
                esac ;;
            frontend)
                case "$cwd" in
                    "$REPO/frontend"|"$REPO/frontend/"*)
                        case "$cmd" in
                            *node*|*npm*|next-server*|*next*) echo "$pid" ;;
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
}
