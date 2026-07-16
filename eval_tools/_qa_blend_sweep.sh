#!/usr/bin/env bash
# 3-arm alpha-sweep blending A/B (Arm C).
# Arm A (split-path, no rerank) results must already exist in logs/h2eval/qa_splitpath.json.
# Runs: blend-0.3, blend-0.5, blend-0.7
# Outputs: logs/h2eval/qa_blend_03.json  qa_blend_05.json  qa_blend_07.json
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
H="$REPO/logs/h2eval"
LOG="$H/qa_blend_sweep.log"
mkdir -p "$H"
: > "$LOG"

stamp() { date "+%H:%M:%S"; }

run_blend() {  # $1=label  $2=alpha
  local lab="$1" alpha="$2"
  echo "[$lab] server up $(stamp)" | tee -a "$LOG"
  ( cd "$REPO" && env LLM_MODEL="qwen3.5:9b" LLM_TEMPERATURE=0 PORT=8000 CHAT_LOG_DISABLED=1 \
      SPLIT_PATH_ENABLED=true RERANK_ENABLED=true RERANK_PREFETCH_K=20 \
      RERANK_BLEND_ALPHA="$alpha" \
      nohup "$PY" project/server.py > "/tmp/srv_${lab}.log" 2>&1 ) &
  local srv_pid=$!
  HEALTHY=0
  for i in $(seq 1 240); do
    sleep 2
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then HEALTHY=1; break; fi
    if ! kill -0 "$srv_pid" 2>/dev/null; then
      echo "[$lab] server DIED $(stamp)" | tee -a "$LOG"; return 1
    fi
  done
  if [ $HEALTHY -eq 0 ]; then echo "[$lab] health timeout $(stamp)" | tee -a "$LOG"; return 1; fi
  grep -m1 "Reranker ready\|RAG system ready" "/tmp/srv_${lab}.log" | tail -1 | tee -a "$LOG" || true
  echo "[$lab] healthy, eval start $(stamp)" | tee -a "$LOG"
  QA_OUT="$H/qa_$lab.json" "$PY" "$REPO/eval_tools/_qa_blend_eval.py" > "/tmp/eval_${lab}.log" 2>&1
  echo "[$lab] eval done $(stamp)" | tee -a "$LOG"
  kill "$srv_pid" 2>/dev/null
  # Kill the server's process group to catch any child processes it spawned.
  # Using the tracked PID's group avoids pkill -f which would match unrelated processes.
  pgid=$(ps -o pgid= -p "$srv_pid" 2>/dev/null | tr -d ' ') && [ -n "$pgid" ] && kill -- "-$pgid" 2>/dev/null || true
  sleep 3
}

run_blend blend_03 0.3
run_blend blend_05 0.5
run_blend blend_07 0.7

echo "" | tee -a "$LOG"
echo "=== BLEND SWEEP DONE $(stamp) ===" | tee -a "$LOG"

# ── quick comparison vs Arm A (split-path) ─────────────────────────────────
QA_H_DIR="$H" "$PY" - <<'PYEOF' | tee -a "$LOG"
import json, collections, os

H = os.environ["QA_H_DIR"]
PROBE_FILE = f"{H}/rank_probe_scenario_v2.json"

A = json.load(open(f"{H}/qa_splitpath.json", encoding="utf-8"))
probe_raw = json.load(open(PROBE_FILE, encoding="utf-8"))
bucket = {r["id"]: r["bucket"] for r in (probe_raw["results"] if isinstance(probe_raw, dict) else probe_raw)}
a_res = {r["id"]: r for r in A["results"]}

ALPHAS = [("03", 0.3), ("05", 0.5), ("07", 0.7)]
REGRESSION_IDS = {1, 6, 15, 17, 45, 55}

def ok_contains(r): return r["verdict"] in ("PASS", "CONTAINS")

print("\n=== BLEND SWEEP vs Arm A (split-path, no rerank) ===")
print(f"{'arm':12}  strict  contains  viol  at_cap8   p50(ms)   p90(ms)")
s = A["summary"]
print(f"{'A split-path':12}  {s['strict']:6}  {s['contains']:8}  {s['must_not_violations']:4}  {s['tool_calls']['at_cap8']:7}  {s['latency_ms']['p50']:9}  {s['latency_ms']['p90']:9}")

for sfx, alpha in ALPHAS:
    try:
        B = json.load(open(f"{H}/qa_blend_{sfx}.json", encoding="utf-8"))
    except FileNotFoundError:
        print(f"blend_{sfx}: FILE NOT FOUND"); continue
    s = B["summary"]
    b_res = {r["id"]: r for r in B["results"]}
    gains = [i for i in a_res if i in b_res and ok_contains(b_res[i]) and not ok_contains(a_res[i])]
    losses = [i for i in a_res if i in b_res and ok_contains(a_res[i]) and not ok_contains(b_res[i])]
    reg_recovered = [i for i in REGRESSION_IDS if i in b_res and ok_contains(b_res[i]) and i in a_res and not ok_contains(a_res[i])]
    print(f"blend-{alpha}:     {s['strict']:6}  {s['contains']:8}  {s['must_not_violations']:4}  {s['tool_calls']['at_cap8']:7}  {s['latency_ms']['p50']:9}  {s['latency_ms']['p90']:9}   NET {len(gains)-len(losses):+d}  reg_recovered={reg_recovered}")

PYEOF
