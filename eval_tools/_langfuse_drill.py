"""Drill into the slowest Langfuse traces — show the observation timeline (the loop)."""
import os, sys, time, requests
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv("project/.env")
BASE = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
AUTH = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
CA = os.environ.get("REQUESTS_CA_BUNDLE")


def get(path, **params):
    for a in range(5):
        r = requests.get(BASE + path, params=params, auth=AUTH, timeout=60, verify=CA)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (a + 1)); continue
        r.raise_for_status(); return r.json()
    raise RuntimeError("gave up")


# find slowest traces
traces = []
for page in range(1, 5):
    traces += get("/api/public/traces", limit=50, page=page)["data"]
traces = [t for t in traces if t.get("latency")]
for t in sorted(traces, key=lambda x: -x["latency"])[:3]:
    tid = t["id"]
    q = str(t.get("input"))[:60]
    print("=" * 72)
    print(f"TRACE {t['latency']:.1f}s  q={q}")
    full = get(f"/api/public/traces/{tid}")
    obs = sorted(full.get("observations", []), key=lambda o: o.get("startTime") or "")
    n_gen = sum(1 for o in obs if o.get("type") == "GENERATION")
    n_search = sum(1 for o in obs if o.get("name") == "search_child_chunks")
    n_parent = sum(1 for o in obs if o.get("name") == "retrieve_parent_chunks")
    print(f"  LLM calls={n_gen}  searches={n_search}  parent_retrieves={n_parent}")
    print("  timeline (name | latency):")
    for o in obs:
        if o.get("type") in ("GENERATION", "TOOL") or o.get("name") in ("orchestrator", "compress_context", "agent", "aggregate_answers"):
            la = o.get("latency")
            print(f"    {str(o.get('name'))[:24]:24} {o.get('type'):11} {(f'{la:.1f}s' if la else '-'):>7}")
