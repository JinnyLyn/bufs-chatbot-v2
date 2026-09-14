# _common.sh — scripts/ 공용 함수 모음 (stack.sh / healthcheck.sh / deploy.sh / setup.sh 가 source).
# 실행하지 않고 source 한다. bash 전용.
#
# 제공:
#   REPO, LOG_DIR, RUN_DIR            레포 기준 경로 (source 시 생성)
#   port_open / wait_port             TCP 리슨 확인 (root·외부 도구 없이)
#   derive_ollama_port                project/.env 의 OLLAMA_BASE_URL 에서 OLLAMA_PORT 도출
#   find_service_pids <name>          pidfile 없이도 우리 스택 프로세스 찾기
#   units_render / units_refresh      scripts/systemd/* 를 이 체크아웃 경로로 렌더·설치본 갱신
#   usage_from_header                 파일 머리 주석을 사용법으로 출력
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
# scripts/setup.sh units) are present. stack.sh start/stop/restart then delegate to
# systemctl instead of spawning processes itself, so there is exactly one way the
# stack runs on a box: either the units own the processes, or the scripts do.
# 유닛(camchat.target)이 이 서버에 설치돼 있나 — 어느 체크아웃 것이든.
units_present() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user cat camchat.target >/dev/null 2>&1
}

# 유닛이 서비스하는 체크아웃 경로 (camchat-backend.service 의 WorkingDirectory). 유닛이 없거나
# 알 수 없으면 빈 값. 테스트 훅: CAMCHAT_UNITS_DIR 가 정의돼 있으면 systemctl 대신 그 값.
units_serving_dir() {
    if [ -n "${CAMCHAT_UNITS_DIR+x}" ]; then echo "$CAMCHAT_UNITS_DIR"; return 0; fi
    units_present || return 0
    systemctl --user show -p WorkingDirectory --value camchat-backend.service 2>/dev/null || true
}

# 유닛이 "이 체크아웃" 것인가. 유닛은 한 폴더(운영)에 묶여 있으니 다른 worktree(staging·개발)에선
# 거짓 — 그러면 stack.sh 가 systemctl 을 건드리지 않고 자기 프로세스만 직접
# 관리한다 (안 그러면 staging 의 stack.sh stop 이 운영을 내린다). 유닛은 있는데 어디 것인지 알 수
# 없으면(빈 값) 역시 거짓 — 남의 것을 systemctl 로 내리느니 직접 관리가 낫다. 한 프로세스 안에선
# 답이 바뀌지 않으니 한 번만 묻는다.
units_installed() {
    if [ -z "${_UNITS_INSTALLED+x}" ]; then
        local wd; wd="$(units_serving_dir)"
        if [ -n "$wd" ] && [ "$wd" = "$REPO" ]; then _UNITS_INSTALLED=0; else _UNITS_INSTALLED=1; fi
    fi
    return "$_UNITS_INSTALLED"
}

# systemd 유닛 파일: 레포의 scripts/systemd/* 가 원본이고, 설치본은 ~/.config/systemd/user (테스트 훅:
# CAMCHAT_UNIT_DIR). 유닛 속 %h/camchat 은 실제 체크아웃 경로로 바꿔 넣는다 — worktree 나 다른 이름의
# 클론도 자기 스크립트를 가리키게.
UNIT_DIR="${CAMCHAT_UNIT_DIR:-$HOME/.config/systemd/user}"
units_render() { sed "s|%h/camchat|$REPO|g" "$1"; }
# scripts/systemd/* 전부를 $1 폴더에 렌더한다.
units_render_all() { local f; for f in "$REPO"/scripts/systemd/*; do units_render "$f" >"$1/${f##*/}"; done; }
# $1 폴더의 유닛 파일을 systemd-analyze 로 검증한다 — 설치하기 전에, 깨진 유닛을 깔지 않게.
units_verify() { systemd-analyze --user verify "$1"/*.service "$1"/*.timer "$1"/*.target; }
# $1 폴더의 유닛을 설치본으로 복사하고 logrotate.conf 도 이 체크아웃용으로 렌더한다 (daemon-reload 는 호출자가).
# 하나라도 못 쓰면 1 — 호출자는 "설치본이 최신" 이라고 믿으면 안 된다.
units_install_from() {
    local f
    mkdir -p "$UNIT_DIR" || return 1
    for f in "$1"/*; do
        cp "$f" "$UNIT_DIR/${f##*/}" && chmod 0644 "$UNIT_DIR/${f##*/}" || return 1
    done
    units_render "$REPO/scripts/logrotate.conf" >"$RUN_DIR/logrotate.conf" || return 1
}
# 설치본의 camchat 유닛 파일 이름들 (unit/timer/target 만 — .bak 같은 건 건드리지 않는다).
units_installed_names() {
    local f
    for f in "$UNIT_DIR"/camchat*; do
        [ -e "$f" ] || continue
        case "$f" in *.service|*.timer|*.target) echo "${f##*/}" ;; esac
    done
}

# 설치된 유닛을 이 체크아웃의 scripts/systemd/* (+ logrotate.conf) 로 맞춘다 — 유닛이 이 폴더를 서비스할
# 때만. 렌더 결과가 설치본과 같으면 아무것도 안 한다. 바뀌었으면 임시 폴더에서 검증하고, 설치본을
# logs/run/units-prev/ 에 치워 둔 뒤 복사 → daemon-reload. 트리에서 사라진 유닛(이름 바꿈·삭제)은 내리고
# 지운다 — 남겨 두면 없는 ExecStart 로 203/EXEC 를 반복한다. deploy.sh(체크아웃 직후·복원 때)와
# stack.sh restart(stop 직전)가 부르므로 scripts/systemd/ 를 고친 PR 이 다음 배포에 저절로 적용된다 —
# 예전엔 install-units.sh 를 손으로 다시 돌려야 했다.
# 반환: 0 갱신함 / 1 할 것 없음 / 2 검증 실패 (아무것도 안 바꿈) / 3 설치 실패 (백업본으로 되돌림)
units_refresh() {
    # 테스트 훅: CAMCHAT_UNITS_DIR 로 "유닛이 이 폴더 것" 을 흉내낼 때는 CAMCHAT_UNIT_DIR (샌드박스 설치 폴더)
    # 까지 있어야 진행한다 — 진짜 ~/.config/systemd/user 를 건드리지 않게.
    if [ -n "${CAMCHAT_UNITS_DIR+x}" ] && [ -z "${CAMCHAT_UNIT_DIR:-}" ]; then return 1; fi
    [ -d "$REPO/scripts/systemd" ] || return 1
    units_installed || return 1
    local f name changed=0 tmp prev="$RUN_DIR/units-prev" stale=()
    for f in "$REPO"/scripts/systemd/*; do
        name="${f##*/}"
        units_render "$f" | cmp -s - "$UNIT_DIR/$name" 2>/dev/null || { changed=1; break; }
    done
    for name in $(units_installed_names); do
        [ -e "$REPO/scripts/systemd/$name" ] || { stale+=("$name"); changed=1; }
    done
    if [ "$changed" = 0 ] && units_render "$REPO/scripts/logrotate.conf" | cmp -s - "$RUN_DIR/logrotate.conf" 2>/dev/null; then
        return 1
    fi
    tmp="$(mktemp -d)"
    units_render_all "$tmp"
    if ! units_verify "$tmp"; then
        rm -rf "$tmp"
        echo "[units] scripts/systemd/ 의 유닛 파일이 검증에 실패했습니다 — 설치본은 그대로 둡니다." >&2
        return 2
    fi
    rm -rf "$prev"; mkdir -p "$prev"
    for name in $(units_installed_names); do cp "$UNIT_DIR/$name" "$prev/"; done
    [ -f "$RUN_DIR/logrotate.conf" ] && cp "$RUN_DIR/logrotate.conf" "$prev/logrotate.conf"
    # 새 파일을 먼저 깔고, 그다음에 트리에 없는 유닛을 지운다 — 복사가 실패해도 지운 것만 남는 상태가 안 되게.
    if ! units_install_from "$tmp"; then
        rm -rf "$tmp"
        for name in $(units_installed_names); do [ -f "$prev/$name" ] && cp "$prev/$name" "$UNIT_DIR/$name"; done
        systemctl --user daemon-reload
        echo "[units] 유닛 파일 설치 실패 ($UNIT_DIR 에 쓸 수 없음) — 이전 설치본으로 되돌렸습니다." >&2
        return 3
    fi
    rm -rf "$tmp"
    for name in "${stale[@]+"${stale[@]}"}"; do
        systemctl --user disable --now "$name" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$name"
        echo "[units] 트리에 없는 유닛 $name 을(를) 내리고 지웠습니다."
    done
    systemctl --user daemon-reload
    echo "[units] 유닛 파일(scripts/systemd/, logrotate.conf)이 바뀌어 설치본을 갱신했습니다 (이전 파일: logs/run/units-prev/)"
    return 0
}

# 유닛이 서비스하는 폴더의 포트 (그 폴더의 scripts/env.local, 기본 8000 3000) — 개발 폴더에서 같은 포트로
# 띄우는 실수를 막을 때 쓴다. 출력: "<backend> <frontend>".
units_serving_ports() {
    local wd; wd="$(units_serving_dir)"
    ( BACKEND_PORT=""; FRONTEND_PORT=""
      # shellcheck disable=SC1090
      if [ -n "$wd" ] && [ -f "$wd/scripts/env.local" ]; then . "$wd/scripts/env.local" >/dev/null 2>&1 || true; fi
      echo "${BACKEND_PORT:-8000} ${FRONTEND_PORT:-3000}" )
}

# 파일 머리 주석(2번째 줄부터 첫 빈 줄까지)을 사용법으로 찍는다. 사용: usage_from_header "${BASH_SOURCE[0]}"
usage_from_header() { sed -n '2,/^$/p' "$1" | sed 's/^# \{0,1\}//'; }

# 릴리스 태그의 모양 (vX.Y.Z, 접미사 -alpha/-beta/-rc 선택). deploy.sh 는 이것만 배포한다;
# "wip" 나 "baseline-0901" 같은 체크포인트 태그는 어디서도 릴리스가 아니다.
RELEASE_TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

# 단계를 아는 버전 정렬: vX.Y.Z-alpha < vX.Y.Z-beta < vX.Y.Z-rc < vX.Y.Z
# (git 기본값은 모든 pre-release 를 정식 뒤로 보낸다). 사용: git "${GIT_VERSIONSORT[@]}" …
GIT_VERSIONSORT=(-c versionsort.suffix=-alpha -c versionsort.suffix=-beta -c versionsort.suffix=-rc)

# 지금 체크아웃된 릴리스 태그 (deploy.sh 는 태그를 detached 로 체크아웃), 없으면 빈 값.
# stack.sh restart 배너와 deploy.sh status 가 같은 것을 릴리스라 부르도록 공유; 한 커밋에
# 릴리스 태그가 여럿이면 최신 것.
head_release_tag() {
    git -C "$REPO" "${GIT_VERSIONSORT[@]}" tag --points-at HEAD --sort=-v:refname 2>/dev/null \
        | grep -E "$RELEASE_TAG_RE" | sed -n 1p || true
}

# Maintenance flag: while logs/run/maintenance exists, `healthcheck.sh cron` skips its
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
# This is what lets `stack.sh stop` work even when a service was started outside
# stack.sh start (systemd, an old shell) and no pidfile exists.
find_service_pids() {
    local name="$1" pid cwd cmd
    for pid in $(pgrep -u "$(id -un)" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        cmd="$({ tr '\0' ' ' < "/proc/$pid/cmdline"; } 2>/dev/null || true)"
        [ -n "$cmd" ] || continue
        case "$name" in
            backend)
                # Must be an actual python interpreter running server.py as ITS SCRIPT:
                # argv[0] a python binary, then interpreter flags (-u, -X utf8, …), then the
                # script path (absolute in this repo, or relative with cwd in this repo).
                # `-c`/`-m` runs are not a script run. A looser "python … project/server.py
                # anywhere in the command line" also matched an editor/tail/grep open on the
                # file, a `ruff check project/server.py`, and (2026-09-14) a `bash -c '…'`
                # whose command TEXT mentioned both — stop then TERM'd the operator's own shell.
                local -a argv=(); local i=1
                read -r -a argv <<<"$cmd"
                [[ "${argv[0]:-}" =~ python[0-9.]*$ ]] || continue
                while [[ "${argv[i]:-}" == -* ]]; do
                    case "${argv[i]}" in
                        -X|-W) i=$((i + 2)) ;;   # 값이 따라오는 플래그
                        -c|-m) continue 2 ;;     # 스크립트 실행이 아님
                        *)     i=$((i + 1)) ;;
                    esac
                done
                case "${argv[i]:-}" in
                    "$REPO/project/server.py") echo "$pid" ;;
                    project/server.py|./project/server.py) if [ "$cwd" = "$REPO" ]; then echo "$pid"; fi ;;
                esac ;;
            frontend)
                # Only real server invocations: the standalone server renames its argv
                # to "next-server (vX)", stack.sh launches `node server.js` / `npm run
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
