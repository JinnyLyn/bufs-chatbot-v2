#!/usr/bin/env bash
# alert.sh — operator notification. Usage: alert.sh "<title>" "<body>"
#
# Posts to ALERT_WEBHOOK_URL (Discord or Slack incoming webhook — both accept the JSON
# sent here) and always appends to logs/alerts.log. The URL is a credential: keep it in
# scripts/env.local (gitignored, sourced by _common.sh), never in the repo. With no URL
# configured the alert is logged only. Never fails the caller.
set -u
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
title="${1:-camchat alert}"; body="${2:-}"
host="$(hostname -s 2>/dev/null || echo host)"
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
line="[$stamp] $title — ${body//$'\n'/ | }"
echo "$line" >>"$LOG_DIR/alerts.log"
url="${ALERT_WEBHOOK_URL:-}"
if [ -z "$url" ]; then
    echo "[alert] (no ALERT_WEBHOOK_URL) $line"
    exit 0
fi
msg="🚨 **maruvis.kr / $host** — $title"$'\n'"$stamp"
[ -n "$body" ] && msg="$msg"$'\n'"$body"
# Discord reads "content", Slack reads "text"; each ignores the other key. Body is capped
# so a long healthcheck dump cannot exceed webhook limits.
payload="$(printf '%s' "${msg:0:1800}" | python3 -c 'import json,sys; m=sys.stdin.read(); print(json.dumps({"content": m, "text": m}, ensure_ascii=False))')"
if ! curl -fsS --max-time 10 -H 'Content-Type: application/json' -d "$payload" "$url" >/dev/null 2>&1; then
    echo "[alert] webhook POST failed — $line"
fi
exit 0
