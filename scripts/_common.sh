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

# True when the per-process systemd user units (scripts/systemd/, installed by
# scripts/install-units.sh) are present. start/stop/restart-all.sh then delegate to
# systemctl instead of spawning processes themselves, so there is exactly one way the
# stack runs on a box: either the units own the processes, or the scripts do.
units_installed() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user cat camchat.target >/dev/null 2>&1 || return 1
    # 유닛은 한 체크아웃(운영 폴더)에 묶여 있다. 다른 worktree(staging, 개발)에서 부르면
    # "유닛 없음" 으로 답해 스크립트가 자기 프로세스를 직접 관리하게 한다 — 안 그러면
    # staging 의 stop-all.sh 가 systemctl 로 운영을 내린다.
    local wd
    wd="$(systemctl --user show -p WorkingDirectory --value camchat-backend.service 2>/dev/null || true)"
    [ -z "$wd" ] || [ "$wd" = "$REPO" ]
}

# 릴리스 태그의 모양 (vX.Y.Z, 접미사 -alpha/-beta/-rc 선택). deploy.sh 는 이것만 배포한다;
# "wip" 나 "baseline-0901" 같은 체크포인트 태그는 어디서도 릴리스가 아니다.
RELEASE_TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

# 단계를 아는 버전 정렬: vX.Y.Z-alpha < vX.Y.Z-beta < vX.Y.Z-rc < vX.Y.Z
# (git 기본값은 모든 pre-release 를 정식 뒤로 보낸다). 사용: git "${GIT_VERSIONSORT[@]}" …
GIT_VERSIONSORT=(-c versionsort.suffix=-alpha -c versionsort.suffix=-beta -c versionsort.suffix=-rc)

# 지금 체크아웃된 릴리스 태그 (deploy.sh 는 태그를 detached 로 체크아웃), 없으면 빈 값.
# restart-all.sh 배너와 deploy.sh status 가 같은 것을 릴리스라 부르도록 공유; 한 커밋에
# 릴리스 태그가 여럿이면 최신 것.
head_release_tag() {
    git -C "$REPO" "${GIT_VERSIONSORT[@]}" tag --points-at HEAD --sort=-v:refname 2>/dev/null \
        | grep -E "$RELEASE_TAG_RE" | sed -n 1p || true
}

# Maintenance flag: while logs/run/maintenance exists, healthcheck-cron.sh skips its
# checks (no auto-restart fights a deliberate stop — doc_sync reindex, a deploy bounce,
# the unit switch-over). Callers pair maint_on with maint_off in an EXIT trap.
MAINT_FLAG="$RUN_DIR/maintenance"
maint_on()  { echo "$$ $(date '+%F %T') ${1:-}" >"$MAINT_FLAG"; }
maint_off() { rm -f "$MAINT_FLAG"; }

# Interpreter for the backend: an explicit PYTHON wins, else the repo venv, else python3 —
# and it must import fastapi, or the backend would crash at startup (bare python3 on this
# box is miniconda without the app's deps). Sets PYTHON; returns 1 with a message otherwise.
resolve_python() {
    if [ -z "${PYTHON:-}" ] && [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; fi
    PYTHON="${PYTHON:-python3}"
    if ! "$PYTHON" -c 'import fastapi' >/dev/null 2>&1; then
        echo "[error] '$PYTHON' cannot import fastapi — the backend would crash at startup." >&2
        echo "        Expected the repo venv at $REPO/.venv (create it, or set PYTHON=...)." >&2
        return 1
    fi
    return 0
}

# The Next.js standalone bundle ships only server code; Next expects .next/static and
# public/ copied in alongside server.js. Remove the previous copies first — `cp -r src dst`
# nests into an existing dst (static/static) instead of replacing it.
# Skipped when the staged copy already matches the current BUILD_ID, so a crash-looping
# unit does not recopy the whole bundle on every restart.
stage_standalone() {
    local fe="$REPO/frontend" sa="$REPO/frontend/.next/standalone" build staged
    build="$(cat "$fe/.next/BUILD_ID" 2>/dev/null || echo unknown)"
    staged="$(cat "$sa/.next/STAGED_BUILD_ID" 2>/dev/null || true)"
    if [ "$build" != unknown ] && [ "$build" = "$staged" ] && [ -d "$sa/.next/static" ]; then
        return 0
    fi
    mkdir -p "$sa/.next"
    rm -rf "$sa/.next/static"
    cp -r "$fe/.next/static" "$sa/.next/static"
    if [ -d "$fe/public" ]; then
        rm -rf "$sa/public"
        cp -r "$fe/public" "$sa/public"
    fi
    echo "$build" >"$sa/.next/STAGED_BUILD_ID"
}

# Listening check with no external tools and no root: try to connect.
port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- 3>&- && return 0
    return 1
}

# PIDs holding an advisory LOCK on $1, one per line; empty when nobody holds one.
# Exists because a lock FILE outliving its owner says nothing: the embedded Qdrant
# lock is an OS-level advisory lock released when the process dies, so "file present"
# and "DB in use" are different questions and only the second one matters.
#
# Reads /proc/locks — the kernel's own lock table, so it answers ownership, not mere
# openness: a backup or a diagnostic that merely OPENS the file is not a holder and
# must not raise the warning (a plain `cp -r qdrant_db` does exactly that). No
# fuser/lsof dependency, no root. Returns 2 when the answer is unknowable (no
# /proc/locks), so callers fall back instead of guessing.
#
# /proc/locks lines look like:
#   117: FLOCK  ADVISORY  WRITE 625682 08:02:71833165 0 EOF
#   118: -> POSIX ADVISORY WRITE 900 08:02:71833165 0 EOF     ← blocked waiter
# field 5 is the pid and field 6 is MAJ:MIN:INODE (hex device, decimal inode).
# An OFD lock reports pid -1 (owned by an open file description, not a process);
# it still means the DB is in use, so it is reported as "?".
lock_holders() {
    local dev ino maj min key
    [ -e "$1" ] || return 0
    [ -r /proc/locks ] || return 2
    dev="$(stat -c %d -- "$1" 2>/dev/null)" || return 2
    ino="$(stat -c %i -- "$1" 2>/dev/null)" || return 2
    # glibc st_dev encoding → the major:minor pair the kernel prints.
    maj=$(( ((dev >> 8) & 0xfff) | ((dev >> 32) & ~0xfff) ))
    min=$(( (dev & 0xff) | ((dev >> 12) & ~0xff) ))
    key="$(printf '%02x:%02x:%d' "$maj" "$min" "$ino")"
    awk -v key="$key" '
        {
            # A blocked waiter prints "->" as the second field, shifting the rest.
            i = ($2 == "->") ? 1 : 0
            if ($(6 + i) == key) print ($(5 + i) == "-1") ? "?" : $(5 + i)
        }
    ' /proc/locks | sort -u
    return 0
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
