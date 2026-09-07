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
# Units are written against %h/camchat; install them pointing at THIS checkout so a
# worktree or a differently named clone gets units that actually run its own scripts.
for f in "$REPO"/scripts/systemd/*; do
    sed "s|%h/camchat|$REPO|g" "$f" >"$UNIT_DIR/$(basename "$f")"
    chmod 0644 "$UNIT_DIR/$(basename "$f")"
    echo "[install] $(basename "$f")  (paths → $REPO)"
done
if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
    echo "[warn]    ALERT_WEBHOOK_URL is not set in scripts/env.local — alerts will only go to logs/alerts.log." >&2
    echo "          Add: export ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/…  (or a Slack incoming webhook)" >&2
fi
systemctl --user daemon-reload
# Verify before enabling. A unit that does not parse must not be enabled/started.
verify_out="$(systemd-analyze --user verify "$UNIT_DIR"/camchat*.service "$UNIT_DIR"/camchat.target "$UNIT_DIR"/camchat*.timer "$UNIT_DIR"/camchat-alert@.service 2>&1)" && verify_rc=0 || verify_rc=$?
[ -n "$verify_out" ] && printf '%s\n' "$verify_out"
if [ "$verify_rc" -ne 0 ]; then
    echo "[error] systemd-analyze verify failed (rc=$verify_rc) — fix the unit files before enabling." >&2
    exit 1
fi
systemctl --user enable camchat.target camchat-healthcheck.timer camchat-logrotate.timer >/dev/null
echo "[enable]  camchat.target + camchat-healthcheck.timer + camchat-logrotate.timer"

# Plain install: start the stack too, unless the old plane (agentic-rag.service / a
# shell-started stack) still owns the ports — then the units could not bind, so say so
# and leave starting to --switch. Enabled-but-not-started units would otherwise sit idle
# until the next reboot (linger keeps the user manager alive for months).
if [ "$switch" != 1 ]; then
    old_plane=0
    for name in backend frontend; do
        if [ -n "$(find_service_pids "$name")" ] && ! systemctl --user is-active --quiet "camchat-$name.service"; then
            echo "[warn]    a $name process not owned by camchat-$name.service is running — rerun with --switch to move it under systemd." >&2
            old_plane=1
        fi
    done
    if [ "$old_plane" = 0 ]; then
        echo "[start]   camchat.target + timers"
        systemctl --user start camchat.target camchat-healthcheck.timer camchat-logrotate.timer
    else
        echo "[note]    units installed and enabled but NOT started (old stack still running) — run: scripts/install-units.sh --switch" >&2
    fi
fi

if [ "$switch" = 1 ]; then
    maint_on "install-units --switch"; trap maint_off EXIT
    if systemctl --user cat agentic-rag.service >/dev/null 2>&1; then
        # Its ExecStop is stop-all.sh, which (units now installed) stops the inactive units
        # and then sweeps the old backend+frontend by identity — process-group TERM, 10 s,
        # KILL, port-freed check. Exactly the path a normal deploy uses.
        echo "[switch]  stopping + disabling agentic-rag.service"
        systemctl --user disable --now agentic-rag.service || true
    fi
    # The old ollama too (stop-all leaves it alone by default); idempotent for the rest.
    echo "[switch]  stopping the old ollama (model reloads under the unit)"
    "$REPO/scripts/stop-all.sh" --with-ollama
    echo "[switch]  starting camchat.target"
    systemctl --user start camchat.target
    systemctl --user start camchat-healthcheck.timer camchat-logrotate.timer
    maint_off; trap - EXIT
fi
echo
systemctl --user --no-pager --no-legend list-units 'camchat-*' 'camchat.target' 2>/dev/null || true
systemctl --user --no-pager list-timers 'camchat-*' 2>/dev/null | head -3 || true
echo
echo "Next: bash scripts/healthcheck.sh   |   logs: journalctl --user -u camchat-backend -n 50, logs/<svc>/"
