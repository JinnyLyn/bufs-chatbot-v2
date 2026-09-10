#!/usr/bin/env bash
# setup-worktrees.sh — 폴더 셋 만들기 (한 번, 다시 돌려도 안전).
#
#   ~/camchat            개발 — 지금 이 폴더. 브랜치 마음대로. (여기서 실행)
#   ~/camchat-staging    항상 origin/main, :8010/:3010, staging.sh up|down
#   ~/camchat-prod       항상 릴리스 태그, :8000/:3000, deploy.sh 만 건드림
#
# 세 폴더는 git worktree — .git(히스토리)은 하나를 같이 쓰고, 파일만 따로 꺼내 둔다.
# 폴더마다 따로 두는 것: project/.env, scripts/env.local, logs/, frontend/node_modules,
# frontend/.next. .venv(9.5 GB)만 개발 폴더 것을 심링크로 공유한다 — 개발 폴더에서
# pip 로 뭘 바꾸면 운영도 같이 바뀌니, 의존성 변경은 PR 로만 (RELEASE.md).
#
# 이 스크립트는 systemd 유닛과 cloudflared 는 건드리지 않는다. 끝나면 다음 단계를 찍어 준다.
#
#   scripts/setup-worktrees.sh
# 환경: PROD_DIR, STAGING_DIR, STAGING_BACKEND_PORT(8010), STAGING_FRONTEND_PORT(3010),
#       DEV_BACKEND_PORT(8020), DEV_FRONTEND_PORT(3020) — 개발 폴더에서 실수로 start-all.sh
#       를 쳐도 운영 포트와 부딪히지 않게 개발 env.local 에 적어 둔다.

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

PROD_DIR="${PROD_DIR:-$HOME/camchat-prod}"
STAGING_DIR="${STAGING_DIR:-$HOME/camchat-staging}"
STAGING_BACKEND_PORT="${STAGING_BACKEND_PORT:-8010}"
STAGING_FRONTEND_PORT="${STAGING_FRONTEND_PORT:-3010}"
DEV_BACKEND_PORT="${DEV_BACKEND_PORT:-8020}"
DEV_FRONTEND_PORT="${DEV_FRONTEND_PORT:-3020}"

die() { echo "[error]  $*" >&2; exit 1; }
say() { echo "$*"; }

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

# append_once <파일> <표식> <내용...>  — 표식 줄이 없을 때만 블록을 덧붙인다
append_once() {
    local file="$1" marker="$2"; shift 2
    grep -qF "$marker" "$file" 2>/dev/null && return 0
    printf '\n%s\n' "$marker" >>"$file"
    printf '%s\n' "$@" >>"$file"
}

add_worktree "$PROD_DIR" prod
add_worktree "$STAGING_DIR" staging

# staging: 다른 포트, ollama 는 운영 것을 씀, Langfuse 트레이스는 staging 환경으로 표시
append_once "$STAGING_DIR/scripts/env.local" "# --- staging (setup-worktrees.sh) ---" \
    "export BACKEND_PORT=$STAGING_BACKEND_PORT" \
    "export FRONTEND_PORT=$STAGING_FRONTEND_PORT" \
    "export START_OLLAMA=no   # 운영 ollama(:11500)를 같이 쓴다 — 여기서 ollama 를 띄우거나 내리지 않는다"
if grep -q '^LANGFUSE_TRACING_ENVIRONMENT=' "$STAGING_DIR/project/.env"; then
    sed -i 's/^LANGFUSE_TRACING_ENVIRONMENT=.*/LANGFUSE_TRACING_ENVIRONMENT=staging/' "$STAGING_DIR/project/.env"
else
    printf '\nLANGFUSE_TRACING_ENVIRONMENT=staging\n' >>"$STAGING_DIR/project/.env"
fi
say "[staging] env.local: :$STAGING_BACKEND_PORT/:$STAGING_FRONTEND_PORT, START_OLLAMA=no; .env: LANGFUSE_TRACING_ENVIRONMENT=staging"

# 개발 폴더: 실수로 start-all.sh 를 쳐도 운영 포트를 잡지 않게
[ -f "$REPO/scripts/env.local" ] || : >"$REPO/scripts/env.local"
append_once "$REPO/scripts/env.local" "# --- dev (setup-worktrees.sh): 운영 포트와 부딪히지 않게 ---" \
    "export BACKEND_PORT=$DEV_BACKEND_PORT" \
    "export FRONTEND_PORT=$DEV_FRONTEND_PORT" \
    "export START_OLLAMA=no"
say "[dev]     env.local: :$DEV_BACKEND_PORT/:$DEV_FRONTEND_PORT, START_OLLAMA=no (운영 포트 보호)"

say
say "폴더 셋 준비됨:"
git -C "$REPO" worktree list | sed 's/^/  /'
say
say "다음 (순서대로, RELEASE.md '처음 한 번'):"
say "  1. 운영 유닛을 $PROD_DIR 로 옮기기 — 재기동 없음, 다음 배포 때부터 새 폴더에서 뜸:"
say "       cd $PROD_DIR && scripts/install-units.sh"
say "  2. 성원이 GitHub Releases 에서 첫 릴리스(v0.1.0-alpha, main) 발행"
say "  3. 첫 배포 (이때 재기동 한 번, 30초쯤):"
say "       cd $PROD_DIR && ./scripts/deploy.sh v0.1.0-alpha"
say "  4. staging 도메인: ~/.cloudflared/config.yml 에 staging.maruvis.kr → :$STAGING_FRONTEND_PORT, /api → :$STAGING_BACKEND_PORT"
say "     + cloudflared tunnel route dns maruvis staging.maruvis.kr   (MIGRATION_H100.md 3-7)"
say "  5. 굴려보기: ./scripts/staging.sh up"
