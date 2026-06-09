#!/bin/sh
# ────────────────────────────────────────────────────────────────────────
# 레포 받은 직후 "한 번만" 돌리는 셋업 스크립트.
# 명령어가 생소하면, 에이전트(Claude)한테 이렇게만 말하세요:
#     "이 레포 처음 셋업해줘 (scripts/setup-dev.sh 실행)"
#
# 직접 돌릴 거면:   bash scripts/setup-dev.sh
# 여러 번 돌려도 안전합니다(같은 설정을 다시 맞출 뿐).
# ────────────────────────────────────────────────────────────────────────
set -e
cd "$(git rev-parse --show-toplevel)"

echo "▶ 1/2  main 직접 커밋 차단 훅 켜는 중..."
git config core.hooksPath .githooks

echo "▶ 2/2  줄바꿈(LF) 자동변환 끄는 중 (Windows/Mac 충돌 방지)..."
git config core.autocrlf false

echo ""
echo "✅ 셋업 완료. 이제 안전하게 작업할 수 있어요."
echo "   • main에 실수로 커밋하면 자동으로 막힙니다."
echo "   • 줄바꿈 때문에 Windows/Mac이 싸우지 않습니다."
echo ""
echo "   다음: 새 작업은 항상 새 브랜치에서. 자세한 건 WORKFLOW.md 참고."
echo "   (프론트엔드 작업 시: cd frontend && nvm use  → Node 버전 자동 맞춤)"
