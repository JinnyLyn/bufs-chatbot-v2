"""Primary rule-based eval: run the in-repo golden dataset (eval_tools/datasets/qa_dataset.json)
against the live agentic-rag chatbot (localhost:8000 SSE) and score each answer.

KPIs (see qa_scorer for definitions):
  - guard      : violation_rate / clean_rate  (must_not_include hard guard)
  - retrieval  : retrieval_recall  (gold_document present in retrieved sources, heuristic)
  - intent     : intent_accuracy   (predicted vs gold_intent; dormant until backend emits intent)
  plus per-category and per-difficulty breakdowns and latency.

Answer *correctness* (must_include) is NOT rule-scored here — judge `expected_answer`
with `_ragas_eval.py`. This runner records answers + the must_not_include guard + recall.

Usage::

    python eval_tools/_eval_qa100.py                # full 100-Q live run
    python eval_tools/_eval_qa100.py --n 10         # first 10 (smoke)
    python eval_tools/_eval_qa100.py --base http://localhost:8000
    python eval_tools/_eval_qa100.py --dry-run      # OFFLINE: validate dataset + print stats, no backend

Outputs: logs/qa100_result.json (summary + per-record) and logs/qa100.jsonl (streaming).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

# Make the sibling qa_scorer importable regardless of CWD (sys.path[0] is this dir when
# run as a script; pythonpath=["eval_tools"] covers pytest).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_scorer  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ask(base: str, question: str, timeout: int = 300) -> dict:
    import requests  # imported lazily so --dry-run needs no deps

    sid = requests.post(base + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = base + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": question})
    done, event = None, None
    with requests.get(url, stream=True, timeout=timeout) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "done":
                done = json.loads(line[5:].strip())
            elif line == "":
                event = None
    return done or {}


def _sources_from(done: dict) -> list[str]:
    """Collect retrieved source filenames/urls from the SSE 'done' payload."""
    srcs = list(done.get("source_urls") or [])
    for r in done.get("results") or []:
        s = r.get("source") if isinstance(r, dict) else None
        if s:
            srcs.append(s)
    return srcs


def dry_run(data: list[dict]) -> None:
    from collections import Counter

    print(f"dataset OK: {len(data)} records @ {qa_scorer.DATASET_PATH}")
    print("  category   :", dict(Counter(x["category"] for x in data)))
    print("  difficulty :", dict(Counter(x["difficulty"] for x in data)))
    print("  documents  :", len(Counter(x["gold_document"] for x in data)), "distinct gold_documents")
    n_mni = sum(1 for x in data if x["must_not_include"])
    print(f"  must_not_include guard active on {n_mni}/{len(data)} records")
    # integrity: a gold expected_answer must never trip its own must_not_include guard
    bad = [x["id"] for x in data if not qa_scorer.score_record(x, x["expected_answer"])["clean"]]
    print(f"  gold self-consistency: {len(data) - len(bad)}/{len(data)} CLEAN"
          + (f"  VIOLATING ids={bad}" if bad else "  (all gold answers pass the guard)"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=0, help="run only the first N questions (0=all)")
    ap.add_argument("--dataset", default=None, help="override dataset path")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true", help="validate dataset + print stats, no backend")
    args = ap.parse_args()

    data = qa_scorer.load_dataset(args.dataset)
    if args.n:
        data = data[: args.n]

    if args.dry_run:
        dry_run(data)
        return

    out = os.path.join(_REPO, "logs", "qa100_result.json")
    jsonl = os.path.join(_REPO, "logs", "qa100.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(f"running {len(data)} questions vs {args.base}", flush=True)
    results = []
    t_start = time.time()
    with open(jsonl, "w", encoding="utf-8") as jf:
        for k, rec in enumerate(data, 1):
            q = rec["question"]
            try:
                done = ask(args.base, q, timeout=args.timeout)
                ans = done.get("answer", "")
            except Exception as e:
                done, ans = {}, f"(ERROR {e})"

            sc = qa_scorer.score_record(rec, ans)
            # Backend currently emits intent:"" for every done event; treat empty as "no
            # prediction" so the intent KPI stays dormant until a real intent predictor ships.
            pred_intent = (done.get("intent") or "").strip() or None
            sources = _sources_from(done)
            dr = qa_scorer.doc_recall(rec["gold_document"], sources)

            out_rec = {
                "id": rec["id"], "category": rec["category"], "difficulty": rec["difficulty"],
                "question": q, "gold_intent": rec["gold_intent"], "gold_document": rec["gold_document"],
                "expected_answer": rec["expected_answer"],
                "must_include": rec["must_include"], "must_not_include": rec["must_not_include"],
                "answer": ans,
                **sc,
                "intent_evaluated": pred_intent is not None,
                "pred_intent": pred_intent,
                "intent_correct": qa_scorer.intent_match(rec["gold_intent"], pred_intent),
                "doc_recall_evaluated": bool(sources),
                "doc_hit": dr["hit"], "matched_sources": dr["matched_sources"], "sources": sources,
                "tool_calls": done.get("tool_calls"), "duration_ms": done.get("duration_ms"),
            }
            results.append(out_rec)
            jf.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
            jf.flush()
            el = (time.time() - t_start) / 60
            viol = sum(1 for r in results if r["verdict"] == "VIOLATION")
            print(f"[{k:3}/{len(data)}] id={rec['id']:3} {sc['verdict']:9} "
                  f"violations={viol}/{len(results)} ({el:.1f}m)", flush=True)

    summary = qa_scorer.summarize(results)
    json.dump({"summary": summary, "results": results}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("report ->", out, flush=True)


if __name__ == "__main__":
    main()
