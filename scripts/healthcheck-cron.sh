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
#   - after a restart the stack gets HC_STARTUP_GRACE_S (default 300 s, the same budget
#     start-all.sh allows for a cold start) before failures count again — otherwise a
#     slow model load would be killed every 4 minutes by its own watchdog
#   - logs/run/maintenance present (doc_sync --restart, restart-all.sh, install-units
#     --switch) → skip entirely; a deliberate stop must not trigger a restart
# State lives in logs/run/healthcheck.state (plain key=value, safe to delete).
# Env: HC_MAX_RESTARTS_PER_HOUR, HC_DRY_RUN=1 (log what would be restarted, do nothing),
#      HC_CHECK_CMD / HC_RESTART_CMD (test hooks: replace the probe / the restart action).
set -u
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
MAX="${HC_MAX_RESTARTS_PER_HOUR:-3}"
GRACE="${HC_STARTUP_GRACE_S:-300}"
STATE="$RUN_DIR/healthcheck.state"
now="$(date +%s)"; stamp="$(date '+%Y-%m-%d %H:%M:%S')"

if [ -f "$MAINT_FLAG" ]; then
    echo "[$stamp] skipped — maintenance flag set ($(cat "$MAINT_FLAG" 2>/dev/null))"
    exit 0
fi

fails=0; status=ok; restarts=""; cooldown_until=0
# shellcheck disable=SC1090
[ -f "$STATE" ] && . "$STATE"
save() { printf 'fails=%s\nstatus=%s\nrestarts="%s"\ncooldown_until=%s\n' "$fails" "$status" "$restarts" "$cooldown_until" >"$STATE"; }

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

down="$(grep -E 'DOWN|unreachable' <<<"$out" | head -3)"
if [ "$now" -lt "${cooldown_until:-0}" ]; then
    # Just restarted: give the cold start its full budget before counting failures.
    echo "[$stamp] still starting (grace $((cooldown_until - now))s left): ${down//$'\n'/ | }"
    status=down; save
    exit 1
fi
fails=$((fails + 1))
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

# A backend the operator stopped on purpose (`systemctl --user stop camchat-backend`)
# is "inactive", not "failed"/"activating" — do not undo that; say so once.
if [ -z "${HC_RESTART_CMD:-}" ] && units_installed \
   && [ "$(systemctl --user is-active camchat-backend.service 2>/dev/null)" = inactive ]; then
    if [ "$status" != stopped ]; then
        "$REPO/scripts/alert.sh" "healthcheck 실패 — camchat-backend 가 수동으로 내려간 상태(inactive)라 자동 재기동하지 않음" "$down"
    fi
    status=stopped; save
    echo "[$stamp] backend unit is inactive (stopped on purpose) — not restarting"
    exit 1
fi
if [ "${HC_DRY_RUN:-0}" = 1 ]; then
    echo "[$stamp] DRY RUN — would restart backend+frontend (restart $((count + 1))/$MAX this hour)"
    status=down; save
    exit 1
fi
# Restart what failed. Backend+frontend always; ollama too only when the backend itself
# answered and just the LLM probe failed (a dead backend also prints an [llm/gpu ] line —
# that is not ollama's fault, and bouncing it would evict the warm model). reset-failed
# first: a unit that hit its StartLimitBurst stays "failed" and ignores a plain restart.
with_ollama=0
grep -q '^\[backend \] ok' <<<"$out" && grep -q '\[llm/gpu \]' <<<"$down" && with_ollama=1
restart_rc=0
if [ -n "${HC_RESTART_CMD:-}" ]; then
    $HC_RESTART_CMD || restart_rc=$?
elif units_installed; then
    units=(camchat-backend.service camchat-frontend.service)
    [ "$with_ollama" = 1 ] && units+=(camchat-ollama.service)
    systemctl --user reset-failed "${units[@]}" 2>/dev/null || true
    systemctl --user restart "${units[@]}" || restart_rc=$?
else
    "$REPO/scripts/restart-all.sh" --no-build $([ "$with_ollama" = 1 ] && echo --with-ollama) || restart_rc=$?
fi
restarts="${restarts:+$restarts }$now"; fails=0; status=down; cooldown_until=$((now + GRACE)); save
if [ "$restart_rc" -ne 0 ]; then
    "$REPO/scripts/alert.sh" "재기동 실패 (rc=$restart_rc) — 수동 확인 필요 ($((count + 1))/$MAX this hour)" "$down"$'\n'"systemctl --user status camchat.target"
    echo "[$stamp] restart FAILED rc=$restart_rc ($((count + 1))/$MAX this hour)"
    exit 1
fi
"$REPO/scripts/alert.sh" "healthcheck 2회 연속 실패 → 재기동: 백엔드·프론트$([ "$with_ollama" = 1 ] && echo '·ollama') ($((count + 1))/$MAX this hour)" "$down"
echo "[$stamp] restarted ($((count + 1))/$MAX this hour)"
exit 1
