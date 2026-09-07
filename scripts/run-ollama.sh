#!/usr/bin/env bash
# run-ollama.sh — ExecStart for camchat-ollama.service. Foreground `ollama serve` pinned to
# the port project/.env dials and to team_b's MIG slice (CUDA_VISIBLE_DEVICES from
# scripts/env.local). Exits 0 without starting anything when .env points at a remote
# ollama (the unit then stays inactive instead of restart-looping). A port already held
# by an ollama started outside systemd makes `ollama serve` fail to bind — that is the
# signal; install-units.sh --switch stops the old instance first.
set -Eeuo pipefail
# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
derive_ollama_port
if [ "$OLLAMA_LOCAL" != 1 ]; then
    echo "[ollama] OLLAMA_BASE_URL in project/.env is remote — nothing to run here."
    exit 0
fi
if [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
    echo "[ollama] refusing to start: CUDA_VISIBLE_DEVICES is not set (shared GPU box)." >&2
    echo "         Put the MIG slice UUID in scripts/env.local." >&2
    exit 1
fi
command -v ollama >/dev/null 2>&1 || { echo "[ollama] 'ollama' not on PATH (scripts/env.local PATH?)" >&2; exit 1; }
echo "[ollama] serving on 127.0.0.1:$OLLAMA_PORT (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
exec env OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama serve
