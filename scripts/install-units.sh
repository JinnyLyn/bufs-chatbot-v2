#!/usr/bin/env bash
# install-units.sh — install/refresh the camchat systemd user units (scripts/systemd/*)
# and, with --switch, move a box from the old one-shot agentic-rag.service to them.
#
#   scripts/install-units.sh            # copy units, daemon-reload, enable target + timer
#   scripts/install-units.sh --switch   # + stop agentic-rag (and its ollama), start camchat.target
#
# What the units give you (reports/CamChat-장애대응.pdf §4):
#   - one service per process (ollama / backend / frontend), Restart=on-failure, 5-per-10-min
#     start limit, logs appended to logs/<svc>/ as before
#   - camchat-healthcheck.timer: scripts/healthcheck-cron.sh every 2 min
#   - camchat-logrotate.timer: daily logrotate of logs/ (50 MB × 5, copytruncate)
#   - the existing scripts keep working: start/stop/restart-all.sh delegate to systemctl
#     when the units are present, doc_sync.sh --restart bounces only the backend
# Requires `loginctl enable-linger $USER` once (already the case on the H100 box).
set -Eeuo pipefail
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
derive_ollama_port   # find_service_pids ollama needs OLLAMA_PORT to recognise the old instance

switch=0
case "${1:-}" in
    "") ;;
    --switch) switch=1 ;;
    *) echo "usage: $0 [--switch]" >&2; exit 2 ;;
esac

command -v systemctl >/dev/null 2>&1 || { echo "systemctl not found — this box has no systemd." >&2; exit 1; }
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for f in "$REPO"/scripts/systemd/*; do
    install -m 0644 "$f" "$UNIT_DIR/$(basename "$f")"
    echo "[install] $(basename "$f")"
done
systemctl --user daemon-reload
# Verify before enabling. A unit that does not parse must not be enabled/started.
verify_out="$(systemd-analyze --user verify "$UNIT_DIR"/camchat*.service "$UNIT_DIR"/camchat.target "$UNIT_DIR"/camchat*.timer 2>&1)" && verify_rc=0 || verify_rc=$?
[ -n "$verify_out" ] && printf '%s\n' "$verify_out"
if [ "$verify_rc" -ne 0 ]; then
    echo "[error] systemd-analyze verify failed (rc=$verify_rc) — fix the unit files before enabling." >&2
    exit 1
fi
systemctl --user enable camchat.target camchat-healthcheck.timer camchat-logrotate.timer >/dev/null
echo "[enable]  camchat.target + camchat-healthcheck.timer + camchat-logrotate.timer"

# Units installed but the old plane (agentic-rag.service / a shell-started stack) still owns
# the ports → the units cannot bind and stop-all.sh's unit branch would stop nothing. Say so.
if [ "$switch" != 1 ]; then
    for name in backend frontend; do
        if [ -n "$(find_service_pids "$name")" ] && ! systemctl --user is-active --quiet "camchat-$name.service"; then
            echo "[warn]    a $name process not owned by camchat-$name.service is running — rerun with --switch to move it under systemd." >&2
        fi
    done
fi

if [ "$switch" = 1 ]; then
    if systemctl --user cat agentic-rag.service >/dev/null 2>&1; then
        echo "[switch]  stopping agentic-rag.service (backend+frontend) and its ollama"
        systemctl --user disable --now agentic-rag.service || true
        # stop-all.sh sees the new units now and would delegate — kill the old processes
        # by identity instead (this is the one place the two planes legitimately meet).
        for name in frontend backend ollama; do
            for pid in $(find_service_pids "$name"); do
                echo "[switch]  stopping old $name pid $pid"
                kill -TERM "$pid" 2>/dev/null || true
            done
        done
        sleep 5
    fi
    echo "[switch]  starting camchat.target"
    systemctl --user start camchat.target
    systemctl --user start camchat-healthcheck.timer camchat-logrotate.timer
fi
echo
systemctl --user --no-pager --no-legend list-units 'camchat-*' 'camchat.target' 2>/dev/null || true
systemctl --user --no-pager list-timers 'camchat-*' 2>/dev/null | head -3 || true
echo
echo "Next: bash scripts/healthcheck.sh   |   logs: journalctl --user -u camchat-backend -n 50, logs/<svc>/"
