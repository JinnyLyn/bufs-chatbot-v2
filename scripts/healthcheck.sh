#!/usr/bin/env bash
# healthcheck.sh — Linux/H100 equivalent of scripts/healthcheck.ps1.
# Probes backend /health + /health/llm and the frontend port. Exits 1 if anything is down.
# Usable from cron / external monitoring.
#
# Env: BACKEND_PORT (default 8000), FRONTEND_PORT (default 3000), PYTHON (default python3)

set -uo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
PYTHON="${PYTHON:-python3}"
ok=0

port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- 3>&- && return 0
    return 1
}

health="$(curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" 2>/dev/null)"
if [ -n "$health" ]; then
    echo "$health" | "$PYTHON" -c '
import json, sys
h = json.load(sys.stdin)
print("[backend ] ok   model={}  ollama={}  kb_docs={}  langfuse={}  uptime={}s".format(
    h.get("model"), h.get("ollama_base_url"), h.get("kb_docs"),
    h.get("langfuse_enabled"), h.get("uptime_s")))
'
else
    echo "[backend ] DOWN"; ok=1
fi

# Ollama is a locally-managed core service on the H100 — an unreachable LLM is an
# outage, so (unlike the .ps1) it fails the check. "no model loaded" stays OK.
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
    print("[llm/gpu ] no model loaded (loads on first query)")
for m in models:
    print("[llm/gpu ] {}  gpu={}%  vram={}MB".format(
        m.get("name"), m.get("gpu_offload_pct"), m.get("vram_mb")))
'; then ok=1; fi
else
    echo "[llm/gpu ] DOWN (no response from /health/llm)"; ok=1
fi

if port_open "$FRONTEND_PORT"; then
    echo "[frontend] ok   :$FRONTEND_PORT"
else
    echo "[frontend] DOWN"; ok=1
fi

[ "$ok" -ne 0 ] && exit 1
echo "ALL OK"
