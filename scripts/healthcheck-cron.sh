#!/usr/bin/env bash
# healthcheck-cron.sh — the every-2-minutes check run by camchat-healthcheck.timer.
#
# Policy (reports/CamChat-장애대응.pdf §4.2):
#   - run scripts/healthcheck.sh (backend /health, /health/llm, frontend port)
#   - 2 consecutive failures → restart backend+frontend (systemd units, or
#     restart-all.sh --no-build when the units are not installed)
#   - more than HC_MAX_RESTARTS_PER_HOUR (default 3) restarts in the last hour → stop
#     restarting, alert "suspended" once, keep checking; auto-restart resumes an hour
#     after the oldest restart ages out
#   - alerts (scripts/alert.sh) only on transitions: first failure, each restart,
#     suspension, recovery — never the same state every 2 minutes
# State lives in logs/run/healthcheck.state (plain key=value, safe to delete).
# Env: HC_MAX_RESTARTS_PER_HOUR, HC_DRY_RUN=1 (log what would be restarted, do nothing),
#      HC_CHECK_CMD / HC_RESTART_CMD (test hooks: replace the probe / the restart action).
set -u
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
MAX="${HC_MAX_RESTARTS_PER_HOUR:-3}"
STATE="$RUN_DIR/healthcheck.state"
now="$(date +%s)"; stamp="$(date '+%Y-%m-%d %H:%M:%S')"

fails=0; status=ok; restarts=""
# shellcheck disable=SC1090
[ -f "$STATE" ] && . "$STATE"
save() { printf 'fails=%s\nstatus=%s\nrestarts="%s"\n' "$fails" "$status" "$restarts" >"$STATE"; }

# Keep only restart timestamps from the last hour.
recent=""
for t in $restarts; do [ $((now - t)) -lt 3600 ] && recent="$recent $t"; done
restarts="${recent# }"
count=$(( $(wc -w <<<"$restarts") ))

out="$(${HC_CHECK_CMD:-"$REPO/scripts/healthcheck.sh"} 2>&1)"; rc=$?
if [ "$rc" -eq 0 ]; then
    if [ "$status" != ok ]; then
        "$REPO/scripts/alert.sh" "복구됨 — healthcheck OK" "$(grep -E '^\[' <<<"$out" | head -4)"
    fi
    fails=0; status=ok; save
    echo "[$stamp] ok"
    exit 0
fi

fails=$((fails + 1))
down="$(grep -E 'DOWN|unreachable' <<<"$out" | head -3)"
echo "[$stamp] FAIL #$fails: ${down//$'\n'/ | }"
if [ "$fails" -lt 2 ]; then
    [ "$status" = ok ] && "$REPO/scripts/alert.sh" "healthcheck 실패 (1/2) — 다음 확인에서도 실패하면 재기동" "$down"
    status=down; save
    exit 1
fi

if [ "$count" -ge "$MAX" ]; then
    if [ "$status" != suspended ]; then
        "$REPO/scripts/alert.sh" "자동 재시작 중단 — 최근 1시간 재시작 $count회 (한도 $MAX)" "$down"$'\n'"수동 확인 필요: bash scripts/healthcheck.sh / systemctl --user status camchat.target"
    fi
    status=suspended; save
    exit 1
fi

if [ "${HC_DRY_RUN:-0}" = 1 ]; then
    echo "[$stamp] DRY RUN — would restart backend+frontend (restart $((count + 1))/$MAX this hour)"
    exit 1
fi
if [ -n "${HC_RESTART_CMD:-}" ]; then
    $HC_RESTART_CMD
elif units_installed; then
    systemctl --user restart camchat-backend.service camchat-frontend.service
else
    "$REPO/scripts/restart-all.sh" --no-build
fi
restarts="${restarts:+$restarts }$now"; fails=0; status=down; save
"$REPO/scripts/alert.sh" "healthcheck 2회 연속 실패 → 백엔드·프론트 재기동 ($((count + 1))/$MAX this hour)" "$down"
echo "[$stamp] restarted ($((count + 1))/$MAX this hour)"
exit 1
