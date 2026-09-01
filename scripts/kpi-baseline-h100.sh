#!/usr/bin/env bash
# kpi-baseline-h100.sh — H100 floor 실측 원커맨드 (N=3 캡처 → baseline-update --set-floors)
#
# h100-fast 프로파일의 FLAG(미측정) floor를 실측값으로 확정하고 KPI 게이트를
# advisory → blocking 으로 전환한다. H100 박스에서, 백엔드가 배포 config
# (MIGRATION_H100.md 3-2의 .env)로 떠 있는 상태에서 repo 루트 기준 실행:
#
#   ./scripts/kpi-baseline-h100.sh
#
# Env: BACKEND_URL (기본 http://localhost:8000), N (기본 3), PYTHON (기본 python3)
#
# 끝나면 커밋해야 하는 파일 (둘 다 커밋 대상 — runs/ 캡처 덤프는 gitignored):
#   eval_tools/kpi_profiles.yaml       (floors 실측값 + gating: blocking)
#   eval_tools/baselines/h100-fast.json

set -euo pipefail
cd "$(dirname "$0")/.."

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
N="${N:-3}"
PYTHON="${PYTHON:-python3}"
PROFILE=h100-fast

# 0) 백엔드 헬스 체크 — 안 떠 있으면 측정 자체가 불가
if ! curl -fsS --max-time 5 "$BACKEND_URL/health" >/dev/null; then
    echo "ERROR: backend not reachable at $BACKEND_URL — ./scripts/start-all.sh 먼저" >&2
    exit 2
fi

CAP_DIR="eval_tools/runs/capture-$(date +%Y%m%d-%H%M%S)-$PROFILE"
mkdir -p "$CAP_DIR"

# 1) N회 라이브 캡처 — 각 run이 남긴 predictions.json을 캡처 디렉토리로 모은다
#    (run 디렉토리 형식: eval_tools/runs/<ts>-<profile>-<sha>/ — 캡처 디렉토리는
#     "-<sha>" 꼬리가 없어 아래 글롭에 걸리지 않는다)
for i in $(seq 1 "$N"); do
    echo "── capture $i/$N ─────────────────────────────────────────"
    "$PYTHON" -m eval_tools.kpi run --profile "$PROFILE" --backend-url "$BACKEND_URL" --seed 42
    latest="$(ls -td eval_tools/runs/*-"$PROFILE"-*/ 2>/dev/null | head -1)"
    if [ -z "$latest" ] || [ ! -f "$latest/predictions.json" ]; then
        echo "ERROR: predictions.json not found under eval_tools/runs/ (latest='$latest')" >&2
        exit 2
    fi
    cp "$latest/predictions.json" "$CAP_DIR/predictions_$i.json"
done

# 2) 베이스라인 시드 + floors 실측 확정 (kpi_profiles.yaml 자동 재작성, advisory→blocking)
"$PYTHON" -m eval_tools.kpi baseline-update --profile "$PROFILE" \
    --from-predictions "$CAP_DIR" --set-floors --temp 0 --seed 42

echo
echo "✅ floors 실측 완료. 이제 커밋할 것:"
echo "   eval_tools/kpi_profiles.yaml       (floors + gating: blocking)"
echo "   eval_tools/baselines/$PROFILE.json"
echo "   (캡처 덤프 $CAP_DIR 는 gitignored — 커밋하지 않는다)"
