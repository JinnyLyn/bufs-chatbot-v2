"""Systematic correct/incorrect analysis via Langfuse logs, with retrieval-vs-generation
attribution. For each question we pull the latest trace's final answer AND the retrieved
contexts (search/parent tool outputs), then check the dataset's `must_include` tokens
(explicit gold facts) against BOTH:
  - token in answer?   -> answer correctness
  - token in context?  -> was the evidence retrieved?
Incorrect + evidence retrieved  => GENERATION error (LLM had it, didn't use it)
Incorrect + evidence missing    => RETRIEVAL error (search/KB didn't surface it)

Dataset: in-repo eval_tools/datasets/qa_dataset.json (via qa_scorer).
"""
import os, sys, time, json, requests
from collections import defaultdict, Counter
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_scorer  # in-repo golden dataset + whitespace-insensitive `contains`

# Populated by main() from project/.env — kept module-level so fetch() can read them.
# Importing this module stays side-effect free: no .env load, no Langfuse network I/O.
BASE = AUTH = CA = None


def fetch(path, want, **params):
    out, page = [], 1
    while len(out) < want:
        d = None
        for a in range(6):
            try:
                r = requests.get(BASE + path, params={"limit": 50, "page": page, **params}, auth=AUTH, timeout=60, verify=CA)
                if r.status_code in (429, 500, 502, 503, 504): time.sleep(2 * (a + 1)); continue
                r.raise_for_status(); d = r.json()["data"]; break
            except requests.exceptions.RequestException: time.sleep(2 * (a + 1))
        if not d: break
        out.extend(d); page += 1
    return out[:want]


def qtext(v):
    if isinstance(v, dict):
        msgs = v.get("messages") or []
        if msgs and isinstance(msgs[-1], dict): return str(msgs[-1].get("content", ""))
        return str(v.get("output") or v)
    return str(v or "")


def atext(v):
    if isinstance(v, dict):
        msgs = v.get("messages") or []
        if msgs and isinstance(msgs[-1], dict): return str(msgs[-1].get("content", ""))
        return str(v.get("output") or v)
    return str(v or "")


def main():
    global BASE, AUTH, CA
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_dotenv("project/.env")
    BASE = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
    AUTH = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    CA = os.environ.get("REQUESTS_CA_BUNDLE")

    print("pulling traces + tool observations from Langfuse...", flush=True)
    traces = fetch("/api/public/traces", 260)
    # latest trace per normalized question
    latest = {}
    for t in traces:
        q = qtext(t.get("input")).strip()
        if not q: continue
        ts = t.get("timestamp") or ""
        if q not in latest or ts > latest[q][0]:
            latest[q] = (ts, t["id"], atext(t.get("output")))

    # tool outputs (contexts) per traceId
    ctx_by_trace = defaultdict(list)
    for name in ("search_child_chunks", "retrieve_parent_chunks"):
        for o in fetch("/api/public/observations", 400, type="TOOL", name=name):
            out = o.get("output")
            ctx_by_trace[o["traceId"]].append(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))

    gt_rows = [r for r in qa_scorer.load_dataset() if r.get("must_include")]

    cat = Counter(); by_intent = defaultdict(Counter); examples = defaultdict(list); matchedQ = 0
    for r in gt_rows:
        q = r["question"].strip()
        if q not in latest: continue
        matchedQ += 1
        _, tid, ans = latest[q]
        ctx = "\n".join(ctx_by_trace.get(tid, []))
        facts = set(r["must_include"])           # required keywords (diagnostic probe, not a rule score)
        if not facts: continue
        in_ans = {f for f in facts if qa_scorer.tokens_present(f, ans)}
        in_ctx = {f for f in facts if qa_scorer.tokens_present(f, ctx)}
        missing = facts - in_ans
        if not missing:
            c = "CORRECT"
        elif missing - in_ctx:          # at least one missing fact NOT in context
            c = "RETRIEVAL_ERR"
        else:                            # all missing facts WERE in context
            c = "GENERATION_ERR"
        cat[c] += 1; by_intent[r.get("gold_intent")][c] += 1
        if c != "CORRECT" and len(examples[c]) < 6:
            examples[c].append((r["id"], r.get("gold_intent"), r["expected_answer"][:40], sorted(missing), sorted(missing & in_ctx)))

    tot = sum(cat.values())
    print(f"\n{'='*72}\nANSWER ATTRIBUTION  (matched {matchedQ} Qs to Langfuse traces, {tot} with facts)")
    for c in ("CORRECT", "GENERATION_ERR", "RETRIEVAL_ERR"):
        print(f"  {c:16} {cat[c]:3}  ({cat[c]/tot*100:.0f}%)")
    print(f"\n{'='*72}\nBY INTENT (correct / generation-err / retrieval-err):")
    for it, cc in sorted(by_intent.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {it:18} ok={cc['CORRECT']:2}  gen_err={cc['GENERATION_ERR']:2}  ret_err={cc['RETRIEVAL_ERR']:2}")
    for c in ("GENERATION_ERR", "RETRIEVAL_ERR"):
        print(f"\n{'='*72}\n{c} examples (missing facts | of-which-were-in-context):")
        for i, it, gt, miss, inctx in examples[c]:
            print(f"  {i:5} [{it}] GT={gt}")
            print(f"        missing={miss}  in_context={inctx}")


if __name__ == "__main__":
    main()
