#!/usr/bin/env bash
# setup.sh — 서버 셋업 (한 번, 다시 돌려도 안전): 폴더 셋과 systemd 유닛.
# (옛 setup-worktrees.sh / install-units.sh 를 한 파일로 합친 것, 2026-09-14.)
#
#   scripts/setup.sh worktrees      # ~/camchat-prod, ~/camchat-staging 을 git worktree 로 만든다 — 개발 폴더(~/camchat)에서
#   scripts/setup.sh units          # scripts/systemd/* 를 ~/.config/systemd/user 에 설치·활성화, 포트가 비어 있으면 camchat.target 기동
#   scripts/setup.sh units --move   # 유닛을 이 체크아웃(worktree)으로 옮기고 여기서 backend+frontend 재기동 (stack.sh restart:
#                                   # 필요하면 프론트 빌드 → 재기동 → /health). ollama 는 그대로. 운영 폴더 전환용
#                                   # (RELEASE.md '폴더 셋') — 한 번의 재기동으로 끝나야 "옛 폴더 프로세스 + 새 폴더 유닛" 이 안 생긴다.
#
# worktrees — 폴더 셋:
#   ~/camchat            개발. 브랜치 마음대로. (여기서 실행)
#   ~/camchat-staging    항상 origin/main, :8010/:3010, staging.sh up|down
#   ~/camchat-prod       항상 릴리스 태그, :8000/:3000, deploy.sh 만 건드림
#   세 폴더는 git worktree — .git(히스토리)은 하나를 같이 쓰고 파일만 따로 꺼내 둔다. 폴더마다 따로 두는 것:
#   project/.env, scripts/env.local, logs/, frontend/node_modules, frontend/.next. .venv(9.5 GB)만 개발 폴더 것을
#   심링크로 공유한다 — 개발 폴더에서 pip 로 뭘 바꾸면 운영도 같이 바뀌니, 의존성 변경은 PR 로만 (RELEASE.md).
#   systemd 유닛과 cloudflared 는 건드리지 않는다. 개발 폴더의 env.local 도 건드리지 않는다 — 유닛이 옮겨가기
#   전까진 운영 유닛이 그 파일을 읽는다. 개발 폴더에서 실수로 기본 포트로 `stack.sh start` 를 치는 건
#   stack.sh 자체가 막는다.
#   환경: PROD_DIR, STAGING_DIR, STAGING_BACKEND_PORT(8010), STAGING_FRONTEND_PORT(3010)
#
# units — 유닛이 주는 것 (reports/CamChat-장애대응.pdf §4):
#   - 프로세스마다 서비스 하나 (ollama / backend / frontend), Restart=on-failure, 15분 10회 시작 제한,
#     로그는 전처럼 logs/<svc>/ 에 append
#   - camchat-healthcheck.timer: `scripts/healthcheck.sh cron` 2분마다
#   - camchat-logrotate.timer: logs/ 를 매일 logrotate (50 MB × 5, copytruncate)
#   - `stack.sh start|stop|restart` 는 유닛이 있으면 systemctl 로 위임하고, `doc_sync.sh --restart` 는 백엔드만 내렸다 올린다
#   - 유닛 파일 속 %h/camchat 은 이 체크아웃 경로로 바꿔 넣는다 (_common.sh units_render). 이후 scripts/systemd/ 가
#     바뀌면 deploy.sh / stack.sh restart 가 알아서 설치본을 갱신한다 (units_refresh) — 다시 돌릴 필요 없다.
#   `loginctl enable-linger $USER` 가 한 번 돼 있어야 한다 (H100 박스는 이미).

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

die() { echo "[error]  $*" >&2; exit 1; }
say() { echo "$*"; }
usage() { usage_from_header "${BASH_SOURCE[0]}"; }

# ---------------------------------------------------------------------------------------
# worktrees
# ---------------------------------------------------------------------------------------
cmd_worktrees() {
    [ $# -eq 0 ] || { echo "usage: $0 worktrees" >&2; exit 2; }
    local PROD_DIR="${PROD_DIR:-$HOME/camchat-prod}" STAGING_DIR="${STAGING_DIR:-$HOME/camchat-staging}"
    local STAGING_BACKEND_PORT="${STAGING_BACKEND_PORT:-8010}" STAGING_FRONTEND_PORT="${STAGING_FRONTEND_PORT:-3010}"

    [ -f "$REPO/.deploy-worktree" ] && die "여기는 $(cat "$REPO/.deploy-worktree") 폴더입니다 — 개발 폴더(~/camchat)에서 실행하세요."
    [ -f "$REPO/project/.env" ] || die "$REPO/project/.env 가 없습니다 — 개발 폴더에서 실행하세요."
    command -v npm >/dev/null 2>&1 || die "npm 이 PATH 에 없습니다 (nvm use)."

    git -C "$REPO" fetch origin --quiet --prune || die "git fetch 실패."

    # add_worktree <경로> <이름>
    add_worktree() {
        local dir="$1" name="$2"
        if [ -e "$dir/.git" ]; then
            say "[$name] 이미 있음: $dir ($(git -C "$dir" log -1 --oneline --no-decorate))"
        else
            [ -e "$dir" ] && die "$dir 가 있는데 git worktree 가 아닙니다 — 치우고 다시."
            git -C "$REPO" worktree add --detach "$dir" origin/main >/dev/null
            say "[$name] worktree 생성: $dir  (origin/main, detached)"
        fi
        printf '%s\n' "$name" >"$dir/.deploy-worktree"

        # .venv 공유 (심링크). 진짜 디렉터리가 이미 있으면 그건 그대로 둔다.
        if [ -d "$REPO/.venv" ] && [ ! -e "$dir/.venv" ]; then
            ln -s "$REPO/.venv" "$dir/.venv"
            say "[$name] .venv → $REPO/.venv (심링크 공유)"
        fi

        [ -f "$dir/project/.env" ] || { cp "$REPO/project/.env" "$dir/project/.env"; say "[$name] project/.env 복사"; }
        if [ ! -f "$dir/scripts/env.local" ]; then
            if [ -f "$REPO/scripts/env.local" ]; then cp "$REPO/scripts/env.local" "$dir/scripts/env.local"; else : >"$dir/scripts/env.local"; fi
            say "[$name] scripts/env.local 복사"
        fi
        mkdir -p "$dir/logs"/{backend,frontend,ollama,run}

        if [ -f "$dir/frontend/package-lock.json" ] && [ ! -d "$dir/frontend/node_modules" ]; then
            say "[$name] npm ci (1분쯤) → $dir/logs/frontend/npm-ci.log"
            (cd "$dir/frontend" && npm ci --no-audit --no-fund >"$dir/logs/frontend/npm-ci.log" 2>&1) \
                || die "npm ci 실패 — $dir/logs/frontend/npm-ci.log"
        fi
    }

    # append_once <파일> <표식정규식> <표식> <내용...>  — 표식(옛 이름 포함)이 없을 때만 블록을 덧붙인다.
    # 옛 setup-worktrees.sh 가 적어 둔 표식도 인정해야 "다시 돌려도 안전" 이 지켜진다 (두 번 붙이면 나중 블록이 이긴다).
    append_once() {
        local file="$1" marker_re="$2" marker="$3"; shift 3
        grep -qE "$marker_re" "$file" 2>/dev/null && return 0
        printf '\n%s\n' "$marker" >>"$file"
        printf '%s\n' "$@" >>"$file"
    }

    add_worktree "$PROD_DIR" prod
    add_worktree "$STAGING_DIR" staging

    # staging: 다른 포트, ollama 는 운영 것을 씀, Langfuse 트레이스는 staging 환경으로 표시
    append_once "$STAGING_DIR/scripts/env.local" '^# --- staging \((setup-worktrees\.sh|setup\.sh worktrees)\) ---' "# --- staging (setup.sh worktrees) ---" \
        "export BACKEND_PORT=$STAGING_BACKEND_PORT" \
        "export FRONTEND_PORT=$STAGING_FRONTEND_PORT" \
        "export START_OLLAMA=no   # 운영 ollama(:11500)를 같이 쓴다 — 여기서 ollama 를 띄우거나 내리지 않는다"
    if grep -q '^LANGFUSE_TRACING_ENVIRONMENT=' "$STAGING_DIR/project/.env"; then
        sed -i 's/^LANGFUSE_TRACING_ENVIRONMENT=.*/LANGFUSE_TRACING_ENVIRONMENT=staging/' "$STAGING_DIR/project/.env"
    else
        printf '\nLANGFUSE_TRACING_ENVIRONMENT=staging\n' >>"$STAGING_DIR/project/.env"
    fi
    say "[staging] env.local: :$STAGING_BACKEND_PORT/:$STAGING_FRONTEND_PORT, START_OLLAMA=no; .env: LANGFUSE_TRACING_ENVIRONMENT=staging"

    say
    say "폴더 셋 준비됨:"
    git -C "$REPO" worktree list | sed 's/^/  /'
    say
    say "다음 (MIGRATION_H100.md 3-7):"
    say "  1. 운영 유닛을 $PROD_DIR 로 옮기고 거기서 재기동 (프론트 빌드 몇 분 + 재기동 30초, 한 번):"
    say "       cd $PROD_DIR && scripts/setup.sh units --move"
    say "  2. 릴리스 배포:  cd $PROD_DIR && ./scripts/deploy.sh <태그>   (태그는 성원이 GitHub Releases 에서 발행)"
    say "  3. staging 도메인: ~/.cloudflared/config.yml 에 staging.maruvis.kr → :$STAGING_FRONTEND_PORT, /api → :$STAGING_BACKEND_PORT"
    say "     + cloudflared tunnel route dns maruvis staging.maruvis.kr"
    say "  4. 굴려보기: ./scripts/staging.sh up"
}

# ---------------------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------------------
cmd_units() {
    local move=0 move_rc=0
    case "${1:-}" in
        "") ;;
        --move) move=1 ;;
        *) echo "usage: $0 units [--move]" >&2; exit 2 ;;
    esac
    [ $# -le 1 ] || { echo "usage: $0 units [--move]" >&2; exit 2; }
    command -v systemctl >/dev/null 2>&1 || die "systemctl 이 없습니다 — 이 서버엔 systemd 가 없습니다."
    derive_ollama_port   # find_service_pids ollama 가 옛 인스턴스를 알아보려면 OLLAMA_PORT 가 필요하다

    # 임시 폴더에 렌더 → 검증 → 설치 (_common.sh 의 units_refresh 와 같은 순서) — 깨진 유닛은 설치본에
    # 닿기 전에 걸린다.
    mkdir -p "$UNIT_DIR"
    local tmp name
    tmp="$(mktemp -d)"
    units_render_all "$tmp"
    if ! units_verify "$tmp"; then
        rm -rf "$tmp"
        die "systemd-analyze verify 실패 — scripts/systemd/ 의 유닛 파일을 고친 뒤 다시 (설치본은 그대로)."
    fi
    units_install_from "$tmp"
    for name in $(units_installed_names); do say "[install] $name  (경로 → $REPO)"; done
    say "[install] logrotate.conf → $RUN_DIR/logrotate.conf"
    rm -rf "$tmp"
    if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
        say "[warn]    scripts/env.local 에 ALERT_WEBHOOK_URL 이 없습니다 — 알림은 logs/alerts.log 에만 남습니다." >&2
        say "          추가: export ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/…  (또는 Slack incoming webhook)" >&2
    fi
    systemctl --user daemon-reload
    systemctl --user enable camchat.target camchat-healthcheck.timer camchat-logrotate.timer >/dev/null
    say "[enable]  camchat.target + camchat-healthcheck.timer + camchat-logrotate.timer"

    if [ "$move" = 1 ]; then
        # 유닛은 이제 이 폴더를 가리킨다(daemon-reload 됨). stack.sh restart 가 units_installed 로 그걸 보고
        # 프론트 빌드 → systemctl stop(옛 폴더 프로세스도 유닛 소속이라 같이 내려감) → start → /health.
        say "[move]    backend+frontend 를 $REPO 에서 재기동 (stack.sh restart)"
        # 종료 코드는 그대로 돌려주되(4 = LLM 확인만 실패, 1 = 백엔드 무응답), 아래 유닛·타이머 상태는 늘 찍는다 —
        # 운영 유닛을 옮긴 직후에 상태도 못 보고 끝나면 안 된다.
        "$REPO/scripts/stack.sh" restart || move_rc=$?
    else
        # 그냥 설치: 스택도 띄운다 — 단, 유닛 소속이 아닌 프로세스(셸에서 띄운 스택)가 포트를 쥐고 있으면
        # 유닛이 바인드하지 못하니 말만 하고 기동은 `stack.sh restart` 에 맡긴다 (정체로 옛 프로세스를 내리고
        # 유닛으로 띄운다). enable 만 하고 안 띄우면 다음 재부팅까지 놀게 된다 (linger 가 매니저를 몇 달이고 살려 둔다).
        local old_plane=0
        for name in backend frontend ollama; do
            [ "$name" = ollama ] && [ "$OLLAMA_LOCAL" != 1 ] && continue
            if [ -n "$(find_service_pids "$name")" ] && ! systemctl --user is-active --quiet "camchat-$name.service"; then
                say "[warn]    camchat-$name.service 소속이 아닌 $name 프로세스가 돌고 있습니다." >&2
                old_plane=1
            fi
        done
        # 정체를 모르는 ollama 라도 포트를 쥐고 있으면 유닛은 crash-loop 한다.
        if [ "$OLLAMA_LOCAL" = 1 ] && port_open "$OLLAMA_PORT" && ! systemctl --user is-active --quiet camchat-ollama.service; then
            say "[warn]    camchat-ollama.service 밖의 무언가가 :$OLLAMA_PORT 에 떠 있습니다." >&2
            old_plane=1
        fi
        if [ "$old_plane" = 0 ]; then
            say "[start]   camchat.target + 타이머"
            systemctl --user start camchat.target camchat-healthcheck.timer camchat-logrotate.timer
        else
            say "[note]    유닛은 설치·활성화했지만 기동하지 않음 (옛 스택이 포트를 쥐고 있음) — 갈아타기: scripts/stack.sh restart --with-ollama" >&2
        fi
    fi
    echo
    systemctl --user --no-pager --no-legend list-units 'camchat-*' 'camchat.target' 2>/dev/null || true
    systemctl --user --no-pager list-timers 'camchat-*' 2>/dev/null | head -3 || true
    echo
    echo "다음: bash scripts/healthcheck.sh   |   로그: journalctl --user -u camchat-backend -n 50, logs/<svc>/"
    return "$move_rc"
}

# ---------------------------------------------------------------------------------------
cmd="${1:-}"; [ $# -eq 0 ] || shift
case "$cmd" in
    worktrees) cmd_worktrees "$@" ;;
    units)     cmd_units "$@" || exit $? ;;
    -h|--help) usage ;;
    "")        usage >&2; exit 2 ;;
    *)         echo "알 수 없는 명령: $cmd" >&2; usage >&2; exit 2 ;;
esac
