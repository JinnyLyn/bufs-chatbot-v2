#!/bin/sh
# ────────────────────────────────────────────────────────────────────────
# SessionStart 훅에서 자동 호출되는 "읽기 전용" 점검.
#   - main이 내 브랜치보다 앞섰는지 fetch해서 확인
#   - 합치면 충돌날지 미리보기 (워킹트리는 건드리지 않음 — merge 안 함)
# 실제 동기화는 .claude/scripts/sync-main.sh 가 따로 합니다.
# 무엇이 실패하든 세션을 막지 않도록 항상 exit 0.
# ────────────────────────────────────────────────────────────────────────

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
[ -z "$branch" ] && exit 0
if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
  exit 0
fi

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
