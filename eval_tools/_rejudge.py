"""Offline re-judge: reuse SAVED generations from a ``_ragas_kpi`` result json and re-score
the 5 RAGAS metrics with a different judge model — no backend, no re-generation.

Why this exists (2026-07-07 실측, issue #86): the judge model choice can FLIP the
retrieval-vs-generation attribution. gemma3:4b scored context_recall as a near-constant
0.90 (92/100 records), so every wrong answer got classified generation-side; re-judging
the SAME answers with gemma4:26b flipped the 7-bucket split to retrieval-side 61.
Re-judging 100 records x 5 metrics ≈ 500 warm calls ≈ 20 min on the H100 — vs a full
generation pass. Output keeps the exact ``ragas_kpi_<tag>_*.json`` schema, so
``_error_buckets.py`` / ``_qa_report.py`` consume it unchanged.

The judge MUST be a different model family than the generator (self-bias, issue #82);
the run aborts if they match. Reasoning judges need ``--no-think`` (default) or they
return empty content and every score parses to -1.

Run::

  python eval_tools/_rejudge.py --in logs/ragas_kpi_full_latest.json --judge-model gemma4:26b
  python eval_tools/_rejudge.py --in logs/ragas_kpi_full_latest.json --n 2 --tag smoke

``--n`` truncates ``results`` in the output too — give smokes their own ``--tag`` so a
full run's ``ragas_kpi_<tag>_latest.json`` never gets silently overwritten by a 2-record one.

Outputs: logs/ragas_kpi_<tag>_<ts>.json and logs/ragas_kpi_<tag>_latest.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ragas_eval as rag  # noqa: E402  (judge prompts + extract_score, verbatim)
import qa_scorer  # noqa: E402
from _ragas_kpi import hmean, judge_ollama  # noqa: E402  (same judge call as the online pass)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="ragas_kpi result json (saved generations)")
    ap.add_argument("--judge-url", default=os.environ.get("OLLAMA_JUDGE_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--judge-model", default="gemma4:26b", help="judge model (MUST differ from the generator)")
    ap.add_argument("--think", dest="think", action="store_true", help="let the judge use reasoning tokens")
    ap.add_argument("--no-think", dest="think", action="store_false", help="force JSON output (default)")
    ap.set_defaults(think=False)
    ap.add_argument("--n", type=int, default=0, help="first N records only (0=all; use for smoke)")
    ap.add_argument("--tag", default="rejudge", help="output filename tag")
    args = ap.parse_args()

    payload = json.load(open(args.inp, encoding="utf-8"))
    results = payload["results"][: args.n] if args.n else payload["results"]
    gen_model = payload.get("generator_model")
    print(f"re-judge | in={args.inp} | judge=ollama:{args.judge_model}@{args.judge_url} "
          f"think={args.think} | n={len(results)} | generator={gen_model}", flush=True)
    if gen_model and gen_model == args.judge_model:
        raise SystemExit(f"[ABORT] judge == generator ({gen_model}) — 자기평가는 자기선호 편향으로 "
                         f"Hallucination을 과소집계한다(#82). 다른 계열 judge를 지정하라.")

    t0 = time.time()
    for i, r in enumerate(results, 1):
        q, ref, ans, ctx = r["question"], r.get("reference", ""), r["answer"], r["context"]
        for m in rag.METRICS:
            sysp, tmpl, _ = rag.METRIC_CONFIG[m]
            prompt = tmpl.format(question=q[:500], context=ctx[:1200], answer=ans[:500], reference=ref[:300])
            try:
                sc, reason = rag.extract_score(judge_ollama(args.judge_url, args.judge_model, args.think, sysp, prompt))
            except Exception as e:  # judge call failed — score -1, surfaced in valid_n below
                sc, reason = -1.0, f"(ERR {str(e)[:80]})"
            r[m], r[m + "_reason"] = sc, reason
        print(f"  judge [{i:3}/{len(results)}] id={r['id']:>4} "
              + " ".join(f"{m.split('_')[0][:4]}={r[m]:.2f}" for m in rag.METRICS)
              + f"  ({(time.time() - t0) / 60:.1f}m)", flush=True)

    # Per-metric stats: failed judge calls (-1) must not silently shrink the mean (same
    # policy as _ragas_kpi) — track valid-n and warn loudly.
    def stat(m):
        vals = [r[m] for r in results if isinstance(r.get(m), (int, float)) and r[m] >= 0]
        return (round(sum(vals) / len(vals), 4) if vals else None), len(vals)

    ragas, valid_n = {}, {}
    for m in rag.METRICS:
        ragas[m], valid_n[m] = stat(m)
    judge_failures = {m: len(results) - valid_n[m] for m in rag.METRICS}

    prec, rec = ragas["context_precision"], ragas["context_recall"]
    kpi = {"Accuracy": ragas["answer_correctness"], "Precision": prec, "Recall": rec,
           "F1": hmean(prec or 0, rec or 0), "Faithfulness": ragas["faithfulness"],
           "AnswerRelevancy": ragas["answer_relevancy"]}
    scorable = [r for r in results if qa_scorer.is_retrievable_gold(r.get("gold_document"))]
    doc_recall_rate = round(sum(1 for r in scorable if r.get("doc_hit")) / len(scorable), 4) if scorable else None
    guard_viol = sum(1 for r in results if r.get("guard") == "VIOLATION")

    print("\n=== KPI (re-judged) ===")
    for k, v in kpi.items():
        bar = ("█" * int((v or 0) * 20) + "░" * (20 - int((v or 0) * 20))) if v is not None else "N/A"
        print(f"  {k:<16} {bar} {v}")
    print(f"\n  doc_recall(string match)={doc_recall_rate}  guard_violations={guard_viol}/{len(results)}")
    total_fail = sum(judge_failures.values())
    if total_fail:
        print(f"[WARN] judge 점수 파싱 실패 {total_fail}건(지표별 {judge_failures}) — 평균에서 제외돼 표본이 "
              f"줄었다. judge 모델/엔드포인트({args.judge_url}) 확인 후 재실행 권장.", flush=True)

    out = dict(payload)
    out.update({"judge": f"ollama:{args.judge_model}", "judge_url": args.judge_url,
                "rejudged_from": args.inp, "n": len(results), "ragas": ragas, "kpi": kpi,
                "valid_n": valid_n, "judge_failures": judge_failures,
                "doc_recall_rate": doc_recall_rate, "guard_violations": guard_viol, "results": results})
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(_REPO, "logs"), exist_ok=True)
    for name in (f"ragas_kpi_{args.tag}_{ts}.json", f"ragas_kpi_{args.tag}_latest.json"):
        with open(os.path.join(_REPO, "logs", name), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\nreport -> logs/ragas_kpi_{args.tag}_{ts}.json  (latest -> logs/ragas_kpi_{args.tag}_latest.json)")


if __name__ == "__main__":
    main()
