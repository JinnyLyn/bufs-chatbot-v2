"""Systematic problem-finding via Langfuse traces (REST API).
Pulls recent traces + observations, aggregates latency / errors / tokens / agent-loop depth."""
import os, sys, json, statistics, requests
from collections import defaultdict, Counter
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv("project/.env")

BASE = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
AUTH = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
CA = os.environ.get("REQUESTS_CA_BUNDLE")


import time
def fetch(path, want, **params):
    out, page = [], 1
    while len(out) < want:
        p = {"limit": 50, "page": page, **params}
        d = None
        for attempt in range(5):
            try:
                r = requests.get(BASE + path, params=p, auth=AUTH, timeout=60, verify=CA)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1)); continue
                r.raise_for_status()
                d = r.json()["data"]; break
            except requests.exceptions.RequestException:
                time.sleep(2 * (attempt + 1))
        if not d:
            break
        out.extend(d); page += 1
    return out[:want]


def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


traces = fetch("/api/public/traces", 200)
obs = fetch("/api/public/observations", 1200)
print(f"pulled {len(traces)} traces, {len(obs)} observations\n" + "=" * 70)

# ── 1. trace latency distribution ──
lat = [t["latency"] for t in traces if t.get("latency")]
print("TRACE LATENCY (s):  n=%d  p50=%.1f  p90=%.1f  p95=%.1f  max=%.1f  min=%.1f"
      % (len(lat), pct(lat, .5), pct(lat, .9), pct(lat, .95), max(lat), min(lat)))
slow = sorted(traces, key=lambda t: -(t.get("latency") or 0))[:6]
print("  slowest traces:")
for t in slow:
    print(f"    {t.get('latency',0):6.1f}s  sess={str(t.get('sessionId'))[:8]}  {str(t.get('input'))[:48]}")

# ── 2. observations by type/name: latency + tokens ──
by = defaultdict(lambda: {"lat": [], "tok": [], "n": 0})
errors = []
for o in obs:
    k = (o.get("type"), o.get("name"))
    g = by[k]; g["n"] += 1
    if o.get("latency"): g["lat"].append(o["latency"])
    if o.get("totalTokens"): g["tok"].append(o["totalTokens"])
    if (o.get("level") or "DEFAULT") != "DEFAULT":
        errors.append((o.get("level"), o.get("name"), o.get("statusMessage"), o.get("traceId")))
print("\n" + "=" * 70 + "\nOBSERVATIONS by type/name (latency s, tokens):")
for k, g in sorted(by.items(), key=lambda x: -sum(x[1]["lat"] or [0])):
    la, to = g["lat"], g["tok"]
    print(f"  {str(k[0]):10} {str(k[1])[:26]:26} n={g['n']:4}  lat p50=%5.1f p90=%5.1f max=%5.1f  tok max=%s"
          % (pct(la, .5), pct(la, .9), max(la) if la else 0, max(to) if to else "-"))

# ── 3. errors / warnings ──
print("\n" + "=" * 70 + f"\nNON-DEFAULT level observations: {len(errors)}")
lvl = Counter(e[0] for e in errors)
print("  by level:", dict(lvl))
for e in errors[:12]:
    print(f"    [{e[0]}] {str(e[1])[:20]:20} {str(e[2])[:90]}")

# ── 4. agent-loop depth: LLM generations per trace ──
gen_per_trace = Counter()
for o in obs:
    if o.get("type") == "GENERATION":
        gen_per_trace[o["traceId"]] += 1
if gen_per_trace:
    vals = list(gen_per_trace.values())
    print("\n" + "=" * 70 + "\nLLM CALLS PER TRACE (agent-loop cost):")
    print(f"  mean={statistics.mean(vals):.1f}  p50={pct(vals,.5)}  p90={pct(vals,.9)}  max={max(vals)}")
    print("  distribution:", dict(sorted(Counter(vals).items())))
