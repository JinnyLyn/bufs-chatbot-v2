"""Run the combined88 rich eval N times with the SAME config to measure run-to-run variance
of the stochastic agent. Without this, single-run before/after deltas (~5 questions) can't be
told apart from noise. Reports per-run strict-pass, mean/min/max, and the set of FLAKY
questions (verdict changes across runs) vs STABLE ones — the stable set is what any A/B
(calendar fix, #15 rewrite ON/OFF, embedding swap) should be measured on.
"""
import subprocess, shutil, json, sys, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.join(HERE, "_eval88_rich.py")
RICH = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\combined88_rich.jsonl"
LOGDIR = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs"
N = int(os.environ.get("VAR_RUNS", "3"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

runs = []
for k in range(1, N + 1):
    print(f"=== RUN {k}/{N} ===", flush=True)
    with open(os.path.join(LOGDIR, f"variance_run{k}.log"), "w", encoding="utf-8") as lf:
        subprocess.run([sys.executable, EVAL], check=True, stdout=lf, stderr=subprocess.STDOUT)
    dst = os.path.join(LOGDIR, f"variance_run{k}.jsonl")
    shutil.copy(RICH, dst)
    runs.append({json.loads(l)["id"]: json.loads(l) for l in open(dst, encoding="utf-8")})
    print(f"  run {k} saved -> {dst}", flush=True)

ids = sorted(runs[0])
ans_ids = [i for i in ids if runs[0][i].get("answerable")]
def strict(v): return v in ("PASS", "REFUSE_OK")

per_run = [sum(strict(r[i]["verdict"]) for i in ids if i in r) for r in runs]
per_run_ans = [sum(strict(r[i]["verdict"]) for i in ans_ids if i in r) for r in runs]
durs = [d for r in runs for i in r for d in [r[i].get("duration_ms")] if d]

print("\n" + "=" * 72)
print(f"RUNS={N}")
print(f"strict_pass (all 89): per-run={per_run}  mean={sum(per_run)/N:.1f}  range=[{min(per_run)},{max(per_run)}]  spread={max(per_run)-min(per_run)}")
print(f"strict_pass (answerable 81): per-run={per_run_ans}  spread={max(per_run_ans)-min(per_run_ans)}")
print(f"avg duration_ms across all runs: {sum(durs)/max(1,len(durs)):.0f}")

flaky, stable_pass, stable_fail = [], [], []
for i in ids:
    verds = [r[i]["verdict"] for r in runs if i in r]
    if len(set(verds)) > 1:
        flaky.append((i, runs[0][i].get("intent"), verds))
    elif strict(verds[0]):
        stable_pass.append(i)
    else:
        stable_fail.append(i)

print(f"\nSTABLE pass:  {len(stable_pass)}   STABLE fail: {len(stable_fail)}   FLAKY: {len(flaky)}  (of {len(ids)})")
print("\nFLAKY questions (verdict varies across identical runs = noise floor):")
for i, it, verds in flaky:
    print(f"  {i:5} ({it}): {verds}")
print(f"\n>>> Measurement floor: a fix must move STABLE-fail questions to pass to count; "
      f"changes within the {len(flaky)} flaky set are noise.")

json.dump({"runs": N, "per_run_strict": per_run, "per_run_strict_answerable": per_run_ans,
           "flaky": [{"id": i, "intent": it, "verdicts": v} for i, it, v in flaky],
           "stable_pass": stable_pass, "stable_fail": stable_fail},
          open(os.path.join(LOGDIR, "variance_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\nsummary -> logs\\variance_summary.json")
