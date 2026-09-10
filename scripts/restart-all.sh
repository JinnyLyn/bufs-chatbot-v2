#!/usr/bin/env bash
# restart-all.sh — 필요하면 재빌드하고 스택을 내렸다 올린다. 릴리스의 정문은 scripts/deploy.sh
# (릴리스 태그 체크아웃 → 이 스크립트 호출 → 운영 중 기록 → 실패 시 롤백, RELEASE.md);
# 이미 체크아웃된 코드를 그냥 재기동할 때만 이걸 직접 돌린다.
#
#   ./scripts/deploy.sh v0.2.0-alpha     # 보통의 릴리스: 태그 -> 체크아웃 -> 이 스크립트
#   ./scripts/restart-all.sh             # 현재 체크아웃 그대로 재기동
#
# 하는 일, 순서대로:
#   1. 배포 점검: 서비스할 커밋을 찍는다. 릴리스 태그면 그걸로 끝; 브랜치면 main 이 아닐 때,
#      origin/main 보다 뒤처졌을 때, 워크트리가 더러울 때(커밋 안 된 수정을 서비스) 경고.
#   2. 필요할 때만 프론트 재빌드: `npm run build` 는 아무것도 멈추기 전에 돈다 — 빌드 중에도
#      옛 스택이 계속 서비스하고, 다운타임은 재기동 순간뿐. "필요" = 프론트 소스가
#      .next/BUILD_ID 보다 새로움 (--build 로 강제, --no-build 로 생략).
#   3. stop-all.sh  — 우리 스택 프로세스를 pidfile 유무와 무관하게 정리 (stop-all.sh 참고).
#   4. start-all.sh — 전부 다시 띄우고 /health 를 기다림.
#   5. /health 와 /health/llm 을 찔러 본다 — "재기동됨" 이 "프로세스 있음" 이 아니라
#      "응답하고 LLM 에도 닿음" 을 뜻하도록.
#
# 플래그: --with-ollama   팀 소유 ollama 도 재기동 (모델 재로드 = 느린 시작)
#         --build         프론트 재빌드 강제
#         --no-build      재빌드 검사 자체를 생략
# 환경: start-all.sh 와 같음 (BACKEND_PORT / FRONTEND_PORT / OLLAMA_PORT / ...).
# 종료 코드: 0 정상; 3 프론트 빌드 실패 (서버 안 건드림); 4 백엔드는 응답하지만 LLM 확인 실패
#           (떠 있음, 성능 저하); 1 재기동 뒤 백엔드 무응답.
#           deploy.sh: 3 → 체크아웃만 복원, 4 → 배포 유지 + 경고, 1 → 롤백.

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${BACKEND_PORT:-8000}"

with_ollama=""; build=auto
for arg in "$@"; do
    case "$arg" in
        --with-ollama) with_ollama="--with-ollama" ;;
        --build)       build=yes ;;
        --no-build)    build=no ;;
        *) echo "usage: $0 [--with-ollama] [--build|--no-build]" >&2; exit 2 ;;
    esac
done

# --- 1) deploy sanity ------------------------------------------------------
branch="$(git -C "$REPO" branch --show-current 2>/dev/null || echo '?')"
head="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
tag="$(head_release_tag)"
behind="$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
at_main_tip=0; [ "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" = "$(git -C "$REPO" rev-parse origin/main 2>/dev/null)" ] && at_main_tip=1
if [ -n "$tag" ]; then
    # 릴리스 태그는 deploy.sh 가 체크아웃한 바로 그것 — main 이 아닌 게 정상.
    echo "[deploy] 릴리스 $tag ($head) 서비스"
elif units_installed; then
    # 운영 폴더인데 릴리스 태그가 아니다 — 사용자가 릴리스 안 된 코드를 보게 된다. 첫 배포 전
    # (install-units.sh --move 직후) 에만 정상.
    echo "[deploy] $head 서비스 (브랜치 '${branch:-detached}')"
    echo "[warn]   운영 폴더인데 릴리스 태그가 아닙니다 — 릴리스는 ./scripts/deploy.sh <태그> 로 (RELEASE.md)."
    [ "$behind" = 0 ] || echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
elif [ "$branch" = main ] || [ "$at_main_tip" = 1 ]; then
    # main 이거나 origin/main 최신을 detached 로 꺼낸 것(staging.sh) — 경고할 게 없다.
    echo "[deploy] main ($head) 서비스"
    [ "$behind" = 0 ] || echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
else
    echo "[deploy] $head 서비스 (브랜치 '${branch:-detached}')"
    echo "[warn]   main 도 릴리스 태그도 아님 — 이 폴더가 '${branch:-$head}' 를 서비스하게 됩니다."
    echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
fi
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
    echo "[warn]   워크트리가 더러움 — 커밋 안 된 코드를 서비스합니다."
fi

# --- 2) frontend rebuild if stale -----------------------------------------
FRONTEND="$REPO/frontend"
BUILD_ID="$FRONTEND/.next/BUILD_ID"
if [ "$build" = auto ]; then
    build=no
    if [ ! -f "$BUILD_ID" ]; then
        build=yes
    else
        stale="$(find "$FRONTEND/src" "$FRONTEND/public" \
                      "$FRONTEND/package.json" "$FRONTEND/package-lock.json" \
                      "$FRONTEND/next.config.ts" "$FRONTEND/tsconfig.json" \
                      -newer "$BUILD_ID" -print -quit 2>/dev/null || true)"
        [ -n "$stale" ] && { echo "[build]  마지막 빌드 이후 프론트 소스가 바뀜 (예: ${stale#"$FRONTEND/"})"; build=yes; }
    fi
fi
if [ "$build" = yes ]; then
    echo "[build]  npm run build (그동안 옛 스택이 계속 서비스) -> logs/frontend/build.log"
    (cd "$FRONTEND" && npm run build >"$LOG_DIR/frontend/build.log" 2>&1) \
        || { echo "[error] 프론트 빌드 실패 — 서버는 건드리지 않았습니다. logs/frontend/build.log 확인"; exit 3; }
fi

# --- 3+4) bounce -----------------------------------------------------------
# 일부러 내렸다 올리는 동안 healthcheck 타이머가 "복구" 하겠다고 끼어들면 안 된다.
maint_on "restart-all"; trap maint_off EXIT
"$REPO/scripts/stop-all.sh" ${with_ollama:+"$with_ollama"}
echo
"$REPO/scripts/start-all.sh"
maint_off; trap - EXIT

# --- 5) end-to-end probe ---------------------------------------------------
echo
if curl -fsS --max-time 10 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
    if curl -fsS --max-time 20 "http://127.0.0.1:$BACKEND_PORT/health/llm" >/dev/null 2>&1; then
        echo "[done]   백엔드 정상, LLM 도 닿음 — $head 운영 중."
    else
        echo "[warn]   백엔드는 떴지만 /health/llm 실패 — LLM 에 닿지 않음 (ollama 확인)." >&2
        exit 4
    fi
else
    echo "[error]  재기동 뒤 /health 무응답 — logs/backend/server.err 확인" >&2
    exit 1
fi
