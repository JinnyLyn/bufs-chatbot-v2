#!/bin/sh
# ────────────────────────────────────────────────────────────────────────
# 현재 작업 브랜치를 최신 main과 동기화 (PR 시점 충돌 최소화용).
# main이 앞서갔으면 받아서 합치고, 충돌이 있으면 "지금" 알려줍니다.
#
#   bash .claude/scripts/sync-main.sh           # 받아서 합침
#   bash .claude/scripts/sync-main.sh --check   # 읽기 전용 점검: 뒤처졌는지 + 합치면 충돌날지 미리보기
#                                               # (워킹트리 안 건드림 — SessionStart 훅이 부름, 늘 exit 0)
#
# 명령어 생소하면 에이전트한테:  "main이랑 동기화해줘 (.claude/scripts/sync-main.sh)"
# ────────────────────────────────────────────────────────────────────────
# --- --check: SessionStart 훅에서 자동 호출되는 읽기 전용 점검. 무엇이 실패하든 세션을 막지 않는다
#     (레포 밖이어도, git 이 없어도 늘 exit 0, stderr 도 내지 않는다).
if [ "${1:-}" = "--check" ]; then
  root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
  cd "$root" 2>/dev/null || exit 0
  branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
  [ -z "$branch" ] && exit 0
  if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then exit 0; fi
  # 오프라인/응답지연으로 세션이 멈추지 않게 timeout (없는 OS면 그냥 진행)
  if command -v timeout >/dev/null 2>&1; then TO="timeout 10"; else TO=""; fi
  $TO git fetch origin --quiet 2>/dev/null || exit 0
  ahead=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  [ "$ahead" = "0" ] && exit 0
  if git merge-tree --write-tree origin/main HEAD >/dev/null 2>&1; then
    msg="충돌 없이 깔끔히 합쳐질 거예요."
  else
    msg="⚠️ 합치면 충돌이 예상돼요 — 일찍 풀수록 쉬워요."
  fi
  echo "🔔 [동기화 알림] 현재 브랜치 '$branch'가 main보다 $ahead 커밋 뒤처져 있어요. $msg"
  echo "   → 'bash .claude/scripts/sync-main.sh' 실행, 또는 에이전트한테 \"main이랑 동기화해줘\"."
  exit 0
fi

set -e
cd "$(git rev-parse --show-toplevel)"
branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")

if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
  echo "ℹ️  지금 '$branch'에 있어요. 동기화는 feature 브랜치에서 하세요."
  exit 0
fi

# 미커밋 변경이 있으면 머지가 꼬이니 먼저 정리하게 안내
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠️  커밋 안 된 변경이 있어요. 먼저 'git commit' 하거나 'git stash' 후 다시 실행하세요."
  exit 1
fi

echo "▶ 최신 main 받는 중..."
git fetch origin

ahead=$(git rev-list --count HEAD..origin/main)
if [ "$ahead" = "0" ]; then
  echo "✅ 이미 최신이에요. main이 앞서간 게 없습니다."
  exit 0
fi

echo "▶ main이 $ahead 커밋 앞서 있어요. 지금 브랜치에 합치는 중..."
if git merge --no-edit origin/main; then
  echo "✅ 동기화 완료. 충돌 없음 — 계속 작업하세요."
else
  echo ""
  echo "⚠️  충돌 발생! 아래 파일을 정리해야 해요:"
  git diff --name-only --diff-filter=U | sed 's/^/   - /'
  echo ""
  echo "   해결: 표시된 파일 열어 <<<<<<< ~ >>>>>>> 부분 정리 → git add <파일> → git commit"
  echo "   모르겠으면 에이전트한테 '충돌 풀어줘' 또는 @JinnyLyn 호출."
  echo "   그냥 되돌리려면: git merge --abort"
  exit 1
fi
