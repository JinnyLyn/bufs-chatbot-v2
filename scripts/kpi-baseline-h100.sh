#!/usr/bin/env bash
# kpi-baseline-h100.sh — H100 floor 실측 원커맨드 (N=3 캡처 → baseline-update --set-floors)
#
# h100-fast 프로파일의 FLAG(미측정) floor를 실측값으로 확정하고 KPI 게이트를
# advisory → blocking 으로 전환한다. H100 박스에서, 백엔드가 배포 config
# (MIGRATION_H100.md 3-2의 .env)로 떠 있는 상태에서 repo 루트 기준 실행:
#
#   ./scripts/kpi-baseline-h100.sh
#
# Env: BACKEND_URL (기본 http://localhost:8000), N (기본 3), PYTHON (기본 python3),
#      SKIP_CONFIG_CHECK=1 (num_ctx 검증 생략 — 의도적으로 다른 운영점을 잴 때만)
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

# 0) 백엔드 헬스 + 운영점 검증 — 베이스라인 STAMP에는 프로파일의 num_ctx가 박히므로
#    (build_stamp는 라이브 값을 안 본다), 백엔드가 실제로 그 config로 떠 있는지
#    여기서 확인하지 않으면 "다른 운영점에서 잰 floor"가 게이트 기준이 되어 버린다.
health_json="$(curl -fsS --max-time 5 "$BACKEND_URL/health")" || {
    echo "ERROR: backend not reachable at $BACKEND_URL — ./scripts/start-all.sh 먼저" >&2
    exit 2
}
live_ctx="$(printf '%s' "$health_json" | "$PYTHON" -c \
    'import json,sys; h=json.load(sys.stdin); print(h.get("num_ctx",""))' 2>/dev/null || true)"
live_model="$(printf '%s' "$health_json" | "$PYTHON" -c \
    'import json,sys; h=json.load(sys.stdin); print(h.get("model",""))' 2>/dev/null || true)"
want_ctx="$("$PYTHON" -c \
    "from eval_tools.kpi.profiles import load_profile; print(load_profile('$PROFILE').num_ctx)")"
echo "backend: $BACKEND_URL  model=$live_model  num_ctx=$live_ctx  (profile expects num_ctx=$want_ctx)"
if [ "${SKIP_CONFIG_CHECK:-0}" != "1" ] && [ "$live_ctx" != "$want_ctx" ]; then
    echo "ERROR: 백엔드 num_ctx($live_ctx) ≠ $PROFILE 프로파일($want_ctx)." >&2
    echo "       배포 config(MIGRATION_H100.md 3-2, LLM_NUM_CTX=$want_ctx)로 백엔드를 다시 띄우고 실행." >&2
    echo "       (의도적으로 다른 운영점을 재려면 SKIP_CONFIG_CHECK=1 — 단, 그 floor는 게이트 기준이 된다)" >&2
    exit 2
fi

CAP_DIR="eval_tools/runs/capture-$(date +%Y%m%d-%H%M%S)-$PROFILE"
mkdir -p "$CAP_DIR"

# 1) N회 라이브 캡처 — 각 run이 남긴 predictions.json을 캡처 디렉토리로 모은다
#    (run 디렉토리 형식: eval_tools/runs/<ts>-<profile>-<sha>/ — 캡처 디렉토리는
#     "-<sha>" 꼬리가 없어 아래 글롭에 걸리지 않는다)
for i in $(seq 1 "$N"); do
    echo "── capture $i/$N ─────────────────────────────────────────"
    rc=0
    "$PYTHON" -m eval_tools.kpi run --profile "$PROFILE" --backend-url "$BACKEND_URL" --seed 42 || rc=$?
    if [ "$rc" -ge 2 ]; then
        # exit 2 = 측정 자체가 실패(ERROR) — 덤프를 믿을 수 없으니 중단
        echo "ERROR: kpi run failed (exit $rc, measurement ERROR) — abort" >&2
        exit "$rc"
    elif [ "$rc" -eq 1 ]; then
        # exit 1 = blocking 게이트의 NO-GO 판정. floors "재측정"이 필요한 상황이
        # 바로 성능이 옛 floor 아래일 때이므로, 판정은 기록만 하고 캡처는 계속한다.
        echo "note: gate verdict NO-GO (exit 1) — 캡처는 계속 (floors 재측정 경로)"
    fi
    # `|| true`: pipefail 아래서 글롭 미스/ls 실패가 여기서 조용히 스크립트를 죽이면
    # 아래 진단 메시지가 영영 안 나온다 (scripts/_common.sh의 동일 패턴 참고)
    latest="$(ls -td eval_tools/runs/*-"$PROFILE"-*/ 2>/dev/null | head -1 || true)"
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
