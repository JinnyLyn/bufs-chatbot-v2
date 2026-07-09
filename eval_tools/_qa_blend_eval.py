"""Live A/B eval over qa_dataset.json (must_include/must_not_include) — captures tool_calls.

Records per-question verdict + tool_calls + duration_ms + answer so the net-effect analysis
(term-drift recovery − rerank regression) rests on deterministic per-question deltas.
Output → logs/h2eval/qa_<label>.json  (set QA_OUT to override).
"""
import json, os, re, sys, statistics, urllib.parse
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("QA_BASE", "http://localhost:8000")
SRC  = os.environ.get("QA_SRC", os.path.join(_HERE, "datasets", "qa_dataset.json"))
OUT  = os.environ.get("QA_OUT", os.path.normpath(
         os.path.join(_HERE, "..", "logs", "h2eval", "qa_result.json")))
os.makedirs(os.path.dirname(OUT), exist_ok=True)


def norm(s): return re.sub(r"\s+", "", s).lower()
def present(p, a): return p in a or norm(p) in norm(a)


def ask(question, timeout=400):
    sid = requests.post(BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": question})
    done, event = None, None
    with requests.get(url, stream=True, timeout=timeout) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line is None: continue
            if line.startswith("event:"): event = line[6:].strip()
            elif line.startswith("data:") and event == "done": done = json.loads(line[5:].strip())
            elif line == "": event = None
    return done or {}


data = json.load(open(SRC, encoding="utf-8"))
if isinstance(data, dict): data = data.get("results") or data.get("data") or []
print(f"running {len(data)} q vs {BASE} (out={OUT})", flush=True)

strict = contains = violated_n = total = 0
results, durations, toolcalls = [], [], []
for k, r in enumerate(data, 1):
    q = r["question"]; inc = r.get("must_include") or []; exc = r.get("must_not_include") or []
    try:
        done = ask(q); ans = done.get("answer", ""); dur = done.get("duration_ms"); tc = done.get("tool_calls")
    except Exception as e:
        done = {}; ans = f"(ERROR {e})"; dur = None; tc = None
    total += 1
    violated = [p for p in exc if present(p, ans)]; hits = [p for p in inc if present(p, ans)]
    is_strict = (len(hits) == len(inc) and inc) and not violated
    is_contains = (len(hits) >= 1) and not violated
    strict += is_strict; contains += is_contains; violated_n += bool(violated)
    if dur is not None: durations.append(dur)
    if tc is not None: toolcalls.append(tc)
    verdict = "PASS" if is_strict else ("CONTAINS" if is_contains else ("VIOLATED" if violated else "FAIL"))
    results.append({"id": r.get("id"), "category": r.get("category"), "verdict": verdict,
                    "tool_calls": tc, "duration_ms": dur, "inc_hits": hits, "violated": violated, "answer": ans})
    print(f"[{k:3}/{len(data)}] {str(r.get('id')):4} {verdict:9} strict={strict} contains={contains} viol={violated_n} tc={tc}", flush=True)


def pct(p, xs):
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))] if xs else None
from collections import Counter
summary = {"total": total, "strict": strict, "strict_rate": round(strict/max(1,total),4),
           "contains": contains, "contains_rate": round(contains/max(1,total),4),
           "must_not_violations": violated_n,
           "tool_calls": {"total": sum(toolcalls), "mean": round(sum(toolcalls)/max(1,len(toolcalls)),3),
                          "dist": dict(sorted(Counter(toolcalls).items())), "at_cap8": sum(1 for t in toolcalls if t>=8)},
           "latency_ms": {"p50": pct(50,durations), "p90": pct(90,durations), "mean": round(statistics.mean(durations)) if durations else None}}
json.dump({"summary": summary, "results": results}, open(OUT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n=== SUMMARY ===\n"+json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
