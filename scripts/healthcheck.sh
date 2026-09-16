#!/usr/bin/env bash
# healthcheck.sh — 스택 점검, 타이머 자동 복구, 운영자 알림.
# (옛 healthcheck.sh / healthcheck-cron.sh / alert.sh 를 한 파일로 합친 것, 2026-09-14.)
#
#   scripts/healthcheck.sh                     # 점검: backend /health + /health/llm + frontend 포트. 하나라도 죽으면 1
#   scripts/healthcheck.sh cron                # camchat-healthcheck.timer 가 2분마다 부르는 자동 복구 (아래 정책)
#   scripts/healthcheck.sh alert <제목> <본문>   # 운영자 알림: 웹훅(Discord/Slack) + logs/alerts.log. 절대 실패하지 않음
#
# 점검: 외부 감시(cron 등)에서도 그대로 쓸 수 있다. 출력의 `DOWN` / `unreachable` 은 cron 모드가 grep 한다.
#   환경: BACKEND_PORT (기본 8000), FRONTEND_PORT (기본 3000), PYTHON (기본 python3)
#         scripts/env.local 이 있으면 먼저 읽는다 — staging 폴더에선 그 포트를 본다.
#
# cron 정책 (reports/CamChat-장애대응.pdf §4.2):
#   - 2회 연속 실패 → backend+frontend 재기동 (systemd 유닛, 없으면 `stack.sh restart --no-build`)
#   - 최근 1시간 재기동이 HC_MAX_RESTARTS_PER_HOUR (기본 3) 회를 넘으면 재기동 중단 — "중단" 알림 한 번,
#     점검은 계속; 가장 오래된 재기동이 1시간을 넘기면 자동 재기동 재개
#   - 알림은 전이 때만: 첫 실패, 재기동마다, 중단, 복구 — 같은 상태를 2분마다 반복하지 않는다
#   - 재기동 뒤 HC_STARTUP_GRACE_S (기본 300초, `stack.sh start` 가 콜드 스타트에 주는 것과 같은 예산) 동안은
#     실패로 세지 않는다 — 안 그러면 느린 모델 로드를 자기 워치독이 4분마다 죽인다
#   - logs/run/maintenance 가 있으면(doc_sync --restart, stack.sh restart, deploy.sh) 통째로 건너뛴다 —
#     일부러 내린 것을 재기동하면 안 된다
#   상태는 logs/run/healthcheck.state (key=value, 지워도 됨).
#   환경: HC_MAX_RESTARTS_PER_HOUR, HC_STARTUP_GRACE_S, HC_DRY_RUN=1 (재기동할 것만 기록하고 안 함),
#         HC_CHECK_CMD / HC_RESTART_CMD (테스트 훅: 점검 / 재기동 명령을 대신함)
#
# alert: ALERT_WEBHOOK_URL (Discord 또는 Slack incoming webhook — 둘 다 여기서 보내는 JSON 을 받는다) 로 POST
#   하고 늘 logs/alerts.log 에 남긴다. URL 은 인증정보 — scripts/env.local (gitignored, _common.sh 가 읽음) 에만.
#   URL 이 없으면 로그만. 같은 제목이 ALERT_DEDUPE_S (기본 10분) 안에 또 오면 로그만 남긴다 — 10번 crash-loop
#   하는 유닛이 웹훅 10개를 쏘면 안 된다. camchat-alert@.service (유닛 OnFailure=) 도 이걸 부른다.

set -uo pipefail

# 호출자가 준 포트가 우선이고, 그다음이 scripts/env.local(_common.sh 가 읽음), 마지막이 기본값 —
# staging.sh status 처럼 다른 폴더의 포트를 찍어 보는 호출이 env.local 에 덮이면 엉뚱한 스택을 점검한다.
_caller_backend_port="${BACKEND_PORT:-}"; _caller_frontend_port="${FRONTEND_PORT:-}"

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${_caller_backend_port:-${BACKEND_PORT:-8000}}"
FRONTEND_PORT="${_caller_frontend_port:-${FRONTEND_PORT:-3000}}"
PYTHON="${PYTHON:-python3}"

usage() { usage_from_header "${BASH_SOURCE[0]}"; }

# ---------------------------------------------------------------------------------------
# 점검
# ---------------------------------------------------------------------------------------
cmd_check() {
    local ok=0 health llm
    health="$(curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null)"
    if [ -n "$health" ]; then
        # 200 인데 JSON 이 아니면(터널·프록시가 대신 답한 경우) 정상이 아니다.
        if ! echo "$health" | "$PYTHON" -c '
import json, sys
h = json.load(sys.stdin)
print("[backend ] ok   model={}  ollama={}  kb_docs={}  langfuse={}  uptime={}s".format(
    h.get("model"), h.get("ollama_base_url"), h.get("kb_docs"),
    h.get("langfuse_enabled"), h.get("uptime_s")))
' 2>/dev/null; then
            echo "[backend ] DOWN (/health 응답이 JSON 이 아님)"; ok=1
        fi
    else
        echo "[backend ] DOWN"; ok=1
    fi

    # ollama 는 이 서버가 직접 관리하는 핵심 서비스 — LLM 에 못 닿으면 장애라 점검 실패다.
    # "no model loaded" 는 정상(첫 질문에 로드).
    llm="$(curl -fsS --max-time 8 "http://127.0.0.1:$BACKEND_PORT/health/llm" 2>/dev/null)"
    if [ -n "$llm" ]; then
        if ! echo "$llm" | "$PYTHON" -c '
import json, sys
l = json.load(sys.stdin)
if l.get("status") != "ok":
    print("[llm/gpu ] ollama unreachable at {}".format(l.get("ollama_base_url")))
    sys.exit(1)
models = l.get("loaded_models") or []
if not models:
    print("[llm/gpu ] 로드된 모델 없음 (첫 질문에 로드)")
for m in models:
    print("[llm/gpu ] {}  gpu={}%  vram={}MB".format(
        m.get("name"), m.get("gpu_offload_pct"), m.get("vram_mb")))
'; then ok=1; fi
    else
        echo "[llm/gpu ] DOWN (/health/llm 무응답)"; ok=1
    fi

    if port_open "$FRONTEND_PORT"; then
        echo "[frontend] ok   :$FRONTEND_PORT"
    else
        echo "[frontend] DOWN"; ok=1
    fi

    [ "$ok" -eq 0 ] || return 1
    echo "ALL OK"
    return 0
}

# ---------------------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------------------
cmd_alert() {
    local title="${1:-camchat alert}" body="${2:-}" host stamp line dedupe last nowts url msg payload
    host="$(hostname -s 2>/dev/null || echo host)"
    stamp="$(date '+%Y-%m-%d %H:%M:%S')"
    line="[$stamp] $title — ${body//$'\n'/ | }"
    echo "$line" >>"$LOG_DIR/alerts.log"
    dedupe="${ALERT_DEDUPE_S:-600}"; last="$RUN_DIR/alert.last"; nowts="$(date +%s)"
    local last_title="" last_ts=""
    if [ -f "$last" ]; then last_title="$(sed -n 1p "$last")"; last_ts="$(sed -n 2p "$last")"; fi
    # 2번째 줄이 숫자가 아니면(잘린 파일, 손으로 고친 파일) 중복 억제를 건너뛴다 — 산술 오류로 매번 stderr 를 더럽히지 않게.
    if [ "$last_title" = "$title" ] && [[ "$last_ts" =~ ^[0-9]+$ ]] && [ $((nowts - last_ts)) -lt "$dedupe" ]; then
        echo "[alert] (${dedupe}초 안 같은 제목 — 로그만) $line"
        return 0
    fi
    printf '%s\n%s\n' "$title" "$nowts" >"$last"
    url="${ALERT_WEBHOOK_URL:-}"
    if [ -z "$url" ]; then
        echo "[alert] (ALERT_WEBHOOK_URL 없음 — 로그만) $line"
        return 0
    fi
    msg="🚨 **maruvis.kr / $host** — $title"$'\n'"$stamp"
    [ -n "$body" ] && msg="$msg"$'\n'"$body"
    # Discord 는 "content", Slack 은 "text" 를 읽고 서로 다른 키는 무시한다. 본문은 잘라서 웹훅 한도를 안 넘게.
    payload="$(printf '%s' "${msg:0:1800}" | python3 -c 'import json,sys; m=sys.stdin.read(); print(json.dumps({"content": m, "text": m}, ensure_ascii=False))')"
    if ! curl -fsS --max-time 10 -H 'Content-Type: application/json' -d "$payload" "$url" >/dev/null 2>&1; then
        echo "[alert] 웹훅 POST 실패 — $line"
    fi
    return 0
}

# ---------------------------------------------------------------------------------------
# cron — 타이머가 부르는 자동 복구
# ---------------------------------------------------------------------------------------
cmd_cron() {
    local MAX="${HC_MAX_RESTARTS_PER_HOUR:-3}" GRACE="${HC_STARTUP_GRACE_S:-300}" STATE="$RUN_DIR/healthcheck.state"
    local now stamp
    now="$(date +%s)"; stamp="$(date '+%Y-%m-%d %H:%M:%S')"

    if [ -f "$MAINT_FLAG" ]; then
        echo "[$stamp] 건너뜀 — maintenance 플래그 있음 ($(cat "$MAINT_FLAG" 2>/dev/null))"
        return 0
    fi

    local fails=0 status=ok restarts="" cooldown_until=0
    # shellcheck disable=SC1090
    [ -f "$STATE" ] && . "$STATE"
    save() { printf 'fails=%s\nstatus=%s\nrestarts="%s"\ncooldown_until=%s\n' "$fails" "$status" "$restarts" "$cooldown_until" >"$STATE"; }

    # 최근 1시간 안의 재기동 시각만 남긴다.
    local recent="" t count
    for t in $restarts; do [ $((now - t)) -lt 3600 ] && recent="$recent $t"; done
    restarts="${recent# }"
    count=$(wc -w <<<"$restarts")

    local out rc
    if [ -n "${HC_CHECK_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        out="$($HC_CHECK_CMD 2>&1)"; rc=$?
    else
        out="$(cmd_check 2>&1)"; rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
        if [ "$status" != ok ]; then
            cmd_alert "복구됨 — healthcheck OK" "$(grep -E '^\[' <<<"$out" | head -4)"
        fi
        fails=0; status=ok; save
        echo "[$stamp] ok"
        return 0
    fi

    local down
    down="$(grep -E 'DOWN|unreachable' <<<"$out" | head -3)"

    # systemd 가 지금 (재)기동 중이거나 켜진 지 GRACE 초가 안 된 유닛은 아직 콜드 스타트 예산 안이다 — 그 위에
    # 재기동을 또 얹지 않는다. (2026-09-08: 유닛 전환 직후 타이머 첫 실행이 16ms 된 백엔드를 실패 #1 로 셌고,
    # 나중 프론트 crash 가 "2회 연속" 이 됐다.)
    unit_starting() {  # $1 = 유닛 → activating 이거나 active 된 지 GRACE 초 미만이면 0
        local st ts age
        st="$(systemctl --user is-active "$1" 2>/dev/null || true)"
        [ "$st" = activating ] && return 0
        [ "$st" = active ] || return 1
        ts="$(systemctl --user show -p ActiveEnterTimestamp --value "$1" 2>/dev/null || true)"
        [ -n "$ts" ] || return 1
        age=$(( now - $(date -d "$ts" +%s 2>/dev/null || echo 0) ))
        [ "$age" -lt "$GRACE" ]
    }
    local starting="" u
    if units_installed; then
        for u in camchat-ollama camchat-backend camchat-frontend; do
            unit_starting "$u.service" && starting="$starting $u"
        done
    fi
    if [ "$now" -lt "${cooldown_until:-0}" ] || [ -n "$starting" ]; then
        echo "[$stamp] 아직 뜨는 중 (${starting:+유닛:$starting }유예): ${down//$'\n'/ | }"
        status=down; save
        return 1
    fi
    fails=$((fails + 1))
    echo "[$stamp] FAIL #$fails: ${down//$'\n'/ | }"
    if [ "$fails" -lt 2 ]; then
        [ "$status" = ok ] && cmd_alert "healthcheck 실패 (1/2) — 다음 확인에서도 실패하면 재기동" "$down"
        status=down; save
        return 1
    fi

    if [ "$count" -ge "$MAX" ]; then
        if [ "$status" != suspended ]; then
            cmd_alert "자동 재시작 중단 — 최근 1시간 재시작 $count회 (한도 $MAX)" "$down"$'\n'"수동 확인 필요: bash scripts/healthcheck.sh / systemctl --user status camchat.target"
        fi
        status=suspended; save
        return 1
    fi

    # 운영자가 일부러 내린 백엔드(`systemctl --user stop camchat-backend`)는 "failed"/"activating" 이 아니라
    # "inactive" — 되돌리지 않고 한 번만 알린다.
    if [ -z "${HC_RESTART_CMD:-}" ] && units_installed \
       && [ "$(systemctl --user is-active camchat-backend.service 2>/dev/null)" = inactive ]; then
        if [ "$status" != stopped ]; then
            cmd_alert "healthcheck 실패 — camchat-backend 가 수동으로 내려간 상태(inactive)라 자동 재기동하지 않음" "$down"
        fi
        status=stopped; save
        echo "[$stamp] backend 유닛이 inactive (일부러 내린 것) — 재기동 안 함"
        return 1
    fi
    if [ "${HC_DRY_RUN:-0}" = 1 ]; then
        echo "[$stamp] DRY RUN — backend+frontend 를 재기동했을 것 (이번 시간 $((count + 1))/$MAX)"
        status=down; save
        return 1
    fi
    # 실패한 것을 재기동한다. backend+frontend 는 늘; ollama 는 백엔드가 응답하는데 LLM 확인만 실패했을 때만
    # (죽은 백엔드도 [llm/gpu ] 줄을 찍는다 — 그건 ollama 탓이 아니고, 재기동하면 따뜻한 모델만 내쫓는다).
    # reset-failed 먼저: StartLimitBurst 에 걸린 유닛은 "failed" 로 남아 그냥 restart 를 무시한다.
    local with_ollama=0 restart_rc=0 units
    grep -q '^\[backend \] ok' <<<"$out" && grep -q '\[llm/gpu \]' <<<"$down" && with_ollama=1
    if [ -n "${HC_RESTART_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        $HC_RESTART_CMD || restart_rc=$?
    elif units_installed; then
        units=(camchat-backend.service camchat-frontend.service)
        [ "$with_ollama" = 1 ] && units+=(camchat-ollama.service)
        systemctl --user reset-failed "${units[@]}" 2>/dev/null || true
        systemctl --user restart "${units[@]}" || restart_rc=$?
    else
        "$REPO/scripts/stack.sh" restart --no-build $([ "$with_ollama" = 1 ] && echo --with-ollama) || restart_rc=$?
    fi
    restarts="${restarts:+$restarts }$now"; fails=0; status=down; cooldown_until=$((now + GRACE)); save
    if [ "$restart_rc" -ne 0 ]; then
        cmd_alert "재기동 실패 (rc=$restart_rc) — 수동 확인 필요 ($((count + 1))/$MAX this hour)" "$down"$'\n'"systemctl --user status camchat.target"
        echo "[$stamp] 재기동 실패 rc=$restart_rc (이번 시간 $((count + 1))/$MAX)"
        return 1
    fi
    cmd_alert "healthcheck 2회 연속 실패 → 재기동: 백엔드·프론트$([ "$with_ollama" = 1 ] && echo '·ollama') ($((count + 1))/$MAX this hour)" "$down"
    echo "[$stamp] 재기동함 (이번 시간 $((count + 1))/$MAX)"
    return 1
}

# ---------------------------------------------------------------------------------------
cmd="${1:-}"; [ $# -eq 0 ] || shift
case "$cmd" in
    "")        cmd_check ;;
    cron)      [ $# -eq 0 ] || { echo "사용법: $0 cron" >&2; exit 2; }; cmd_cron ;;
    alert)     cmd_alert "$@" ;;
    -h|--help) usage ;;
    *)         echo "알 수 없는 명령: $cmd" >&2; usage >&2; exit 2 ;;
esac
