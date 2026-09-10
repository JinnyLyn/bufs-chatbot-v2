#!/usr/bin/env bash
# staging.sh — "머지한 거 굴려보는" 두 번째 스택. ~/camchat-staging 을 origin/main 최신으로
# 맞추고 :8010/:3010 에 띄운다 (staging.maruvis.kr). 운영(:8000/:3000, ~/camchat-prod)은
# 절대 건드리지 않는다 — 폴더도, 포트도, systemd 유닛도 다르다.
#
#   ./scripts/staging.sh up       # origin/main 최신 → 필요하면 npm ci·프론트 빌드 → 띄움 (몇 분)
#   ./scripts/staging.sh down     # 내림 (평소엔 내려 두면 자원 0)
#   ./scripts/staging.sh status   # 어느 커밋이 떠 있는지, origin/main 보다 뒤졌는지, 헬스
#
# 환경: STAGING_DIR (기본 $HOME/camchat-staging — scripts/setup-worktrees.sh 가 만든다)
#       STAGING_REF (기본 origin/main)
# staging 폴더의 scripts/env.local 이 포트·START_OLLAMA=no 를 정한다 (ollama 는 운영 것을
# 같이 쓴다 — LLM 은 복제하지 않는다). 프론트의 /api 프록시 대상은 빌드 때 박히므로
# BACKEND_ORIGIN 을 그 포트로 넣고 restart-all.sh 를 부른다.
#
# 자동 복구·healthcheck 타이머 없음: 시험용이라 죽으면 죽은 채로 둔다 (`up` 이 다시 띄운다).
#
# 테스트 훅: STAGING_RESTART_CMD / STAGING_STOP_CMD 가 staging 폴더의 restart-all.sh /
# stop-all.sh 를 대신한다.

set -Eeuo pipefail

STAGING_DIR="${STAGING_DIR:-$HOME/camchat-staging}"
STAGING_REF="${STAGING_REF:-origin/main}"

die() { echo "[error]  $*" >&2; exit 1; }
say() { echo "$*"; }
g() { git -C "$STAGING_DIR" "$@"; }

check_dir() {
    [ -e "$STAGING_DIR/.git" ] || die "staging 폴더가 없습니다: $STAGING_DIR — 먼저 scripts/setup-worktrees.sh"
    [ -f "$STAGING_DIR/.deploy-worktree" ] || die "$STAGING_DIR 는 setup-worktrees.sh 가 만든 폴더가 아닙니다 (.deploy-worktree 마커 없음)."
    # 마커 내용까지 본다 — STAGING_DIR 가 운영 폴더를 가리키면 down 이 운영을 내리고 up 이 태그를 벗긴다.
    [ "$(cat "$STAGING_DIR/.deploy-worktree")" = staging ] \
        || die "$STAGING_DIR 는 staging 폴더가 아닙니다 (마커: $(cat "$STAGING_DIR/.deploy-worktree")) — STAGING_DIR 확인."
}

# staging 폴더의 env.local 에서 포트만 읽는다 (다른 export 는 이 셸에 남기지 않도록 서브셸).
staging_ports() {
    ( # shellcheck disable=SC1091
      [ -f "$STAGING_DIR/scripts/env.local" ] && . "$STAGING_DIR/scripts/env.local"
      echo "${BACKEND_PORT:-8010} ${FRONTEND_PORT:-3010}" )
}

cmd_up() {
    check_dir
    local bport fport
    read -r bport fport < <(staging_ports)

    say "[staging] $STAGING_REF 최신으로"
    g fetch origin --quiet --prune || die "git fetch 실패 — 네트워크/origin 확인."
    # detached: 개발 폴더가 main 을 체크아웃하고 있어도 충돌하지 않고, 여기선 아무도 커밋하지 않는다.
    g checkout --quiet --detach "$STAGING_REF" || die "git checkout $STAGING_REF 실패."
    say "[staging] $(g log -1 --oneline --no-decorate)"

    # 의존성: lock 파일이 node_modules 보다 새로우면 npm ci (1분쯤). 프론트가 없는 체크아웃은 건너뜀.
    if [ -f "$STAGING_DIR/frontend/package-lock.json" ]; then
        local nm="$STAGING_DIR/frontend/node_modules/.package-lock.json"
        if [ ! -f "$nm" ] || [ "$STAGING_DIR/frontend/package-lock.json" -nt "$nm" ]; then
            say "[staging] npm ci (package-lock 이 바뀜)"
            (cd "$STAGING_DIR/frontend" && npm ci --no-audit --no-fund >"$STAGING_DIR/logs/frontend/npm-ci.log" 2>&1) \
                || die "npm ci 실패 — $STAGING_DIR/logs/frontend/npm-ci.log"
        fi
    fi

    # restart-all.sh 가 알아서: 소스가 바뀌었으면 프론트 재빌드(BACKEND_ORIGIN 으로 /api 대상 고정)
    # → stop → start → /health 확인. 유닛은 운영 폴더 것이라 여기선 프로세스를 직접 띄운다.
    say "[staging] restart-all.sh (:$bport / :$fport)"
    export BACKEND_ORIGIN="http://localhost:$bport"
    if [ -n "${STAGING_RESTART_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        $STAGING_RESTART_CMD
    else
        "$STAGING_DIR/scripts/restart-all.sh"
    fi
    say
    say "[staging] 떠 있음 — https://staging.maruvis.kr  (로컬: http://127.0.0.1:$fport)"
    say "          다 봤으면: ./scripts/staging.sh down"
}

cmd_down() {
    check_dir
    if [ -n "${STAGING_STOP_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        $STAGING_STOP_CMD
    else
        "$STAGING_DIR/scripts/stop-all.sh"
    fi
    say "[staging] 내림"
}

cmd_status() {
    check_dir
    local bport fport behind
    read -r bport fport < <(staging_ports)
    g fetch origin --quiet --prune 2>/dev/null || true
    say "[staging] 체크아웃: $(g log -1 --oneline --no-decorate)"
    behind="$(g rev-list --count "HEAD..$STAGING_REF" 2>/dev/null || echo '?')"
    if [ "$behind" = 0 ]; then
        say "[staging] $STAGING_REF 와 같음"
    else
        say "[staging] $STAGING_REF 보다 $behind 커밋 뒤 — ./scripts/staging.sh up 으로 갱신"
    fi
    if [ -x "$STAGING_DIR/scripts/healthcheck.sh" ]; then
        BACKEND_PORT="$bport" FRONTEND_PORT="$fport" "$STAGING_DIR/scripts/healthcheck.sh" 2>&1 | sed 's/^/[health]  /' || true
    fi
}

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
    up)          cmd_up ;;
    down)        cmd_down ;;
    status)      cmd_status ;;
    ""|-h|--help) usage ;;
    *)           usage >&2; exit 2 ;;
esac
