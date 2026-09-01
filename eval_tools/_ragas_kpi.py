"""RAGAS eval + KPI mapping in a single generation pass (LLM-as-judge, separate models).

Complements ``_ragas_eval.py``: same judge prompts (imported verbatim) but the generation
pass ALSO captures retrieval facts (sources, doc_recall, must_not_include guard) so the
7-bucket error analysis (``_error_buckets.py``) and the report (``_qa_report.py``) need no
second generation. Generation = the chatbot backend (localhost:8000 SSE); judge = a DIFFERENT
Ollama model than the generator (no self-bias), reachable at ``--judge-url``.

Reasoning judges (e.g. gemma/qwen3 "thinking" models) spend the whole token budget on hidden
reasoning and return empty ``content`` — pass ``--no-think`` (default) to force JSON output.

KPI mapping (Accuracy / Precision / Recall / F1 / Faithfulness):
  Accuracy = mean(answer_correctness) ; Precision = mean(context_precision)
  Recall   = mean(context_recall)     ; F1 = harmonic_mean(Precision, Recall)
  Faithfulness = mean(faithfulness)   (+ answer_relevancy reported)

Run::

  python eval_tools/_ragas_kpi.py --judge-model gemma4:26b --judge-url http://127.0.0.1:11434 --n 100
  python eval_tools/_ragas_kpi.py --dataset eval_tools/datasets/qa_dataset_factual100.json --tag factual100

Outputs: logs/ragas_kpi_<tag>_<ts>.json and a stable logs/ragas_kpi_<tag>_latest.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ragas_eval as rag  # noqa: E402  (judge prompts + extract_score, verbatim)
import qa_scorer  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ask_full(base: str, q: str, timeout: int = 300) -> dict:
    import requests

    sid = requests.post(base + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = base + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": q})
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


def _sources(done: dict) -> list[str]:
    srcs = list(done.get("source_urls") or [])
    for r in done.get("results") or []:
        s = r.get("source") if isinstance(r, dict) else None
        if s:
            srcs.append(s)
    return srcs


def _ctx(done: dict) -> str:
    return "\n\n".join(r.get("text", "") for r in (done.get("results") or []) if r.get("text"))


def judge_ollama(url: str, model: str, think: bool, system: str, prompt: str) -> str:
    import requests

    r = requests.post(f"{url}/api/chat", timeout=180, json={
        "model": model, "stream": False, "think": think,
        "options": {"temperature": 0, "num_predict": 256},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def hmean(a: float, b: float) -> float:
    return round(2 * a * b / (a + b), 4) if (a and b and a + b > 0) else 0.0


def backend_model(base: str) -> str | None:
    """Best-effort generator model from /health, to verify judge != generator (self-bias guard)."""
    import requests

    try:
        r = requests.get(base + "/health", timeout=10)
        r.raise_for_status()
        return r.json().get("model")
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000", help="chatbot backend (generation)")
    ap.add_argument("--judge-url",
                    default=(os.environ.get("OLLAMA_JUDGE_URL") or os.environ.get("OLLAMA_BASE_URL")
                             or "http://127.0.0.1:11434"))
    ap.add_argument("--judge-model", default="gemma4:26b", help="judge model (MUST differ from the generator)")
    ap.add_argument("--think", dest="think", action="store_true", help="let the judge use reasoning tokens")
    ap.add_argument("--no-think", dest="think", action="store_false", help="force JSON output (default; needed for reasoning judges)")
    ap.set_defaults(think=False)
    ap.add_argument("--dataset", default=None, help="dataset path (default: in-repo qa_dataset.json)")
    ap.add_argument("--n", type=int, default=0, help="first N (0=all)")
    ap.add_argument("--tag", default="qa", help="output filename tag")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    data = qa_scorer.load_dataset(args.dataset)
    if args.n:
        data = data[: args.n]
    print(f"RAGAS+KPI | gen=backend({args.base}) | judge=ollama:{args.judge_model}@{args.judge_url} "
          f"think={args.think} | n={len(data)}", flush=True)

    # Misconfiguration guard: the whole point is a judge model DIFFERENT from the generator
    # (self-eval bias, issue #82). Verify against the backend's /health, fail loud if unverifiable.
    gen_model = backend_model(args.base)
    if gen_model is None:
        print(f"[WARN] 백엔드 /health 확인 실패({args.base}) — 생성 모델 미확인이라 judge≠generator 검증 불가.", flush=True)
    elif gen_model == args.judge_model:
        raise SystemExit(f"[ABORT] judge 모델이 생성 모델과 동일({gen_model}) — 자기평가 편향. "
                         f"--judge-model 을 다른 모델로 지정하라 (issue #82).")
    else:
        print(f"  생성 모델(/health)={gen_model}  ≠ judge={args.judge_model}  ✓", flush=True)

    # Phase 1 — generation (single pass captures answer + context + sources + doc_recall + guard)
    print("Phase 1 — generation", flush=True)
    gen, t0 = [], time.time()
    for i, r in enumerate(data, 1):
        try:
            done = ask_full(args.base, r["question"], timeout=args.timeout)
            ans, ctx = done.get("answer", ""), _ctx(done)
        except Exception as e:
            done, ans, ctx = {}, f"(ERR {e})", ""
        srcs = _sources(done)
        dr = qa_scorer.doc_recall(r["gold_document"], srcs)
        guard = qa_scorer.score_record(r, ans)
        gen.append({**r, "_ans": ans, "_ctx": ctx, "_srcs": srcs, "_doc_hit": dr["hit"],
                    "_matched": dr["matched_sources"], "_guard": guard["verdict"], "_dur": done.get("duration_ms")})
        print(f"  gen [{i:3}/{len(data)}] id={r['id']:>4} ans={len(ans)}자 ctx={len(ctx)} "
              f"doc_hit={dr['hit']} guard={guard['verdict'][:4]} ({(time.time()-t0)/60:.1f}m)", flush=True)

    gen_errors = sum(1 for g in gen if str(g["_ans"]).startswith("(ERR"))
    if gen_errors == len(gen):
        raise SystemExit(f"[ABORT] 생성 {gen_errors}/{len(gen)}건 전량 실패 — 백엔드({args.base})/터널 확인. "
                         f"judge 단계 진행 무의미하므로 중단.")
    if gen_errors:
        print(f"[WARN] 생성 실패 {gen_errors}/{len(gen)}건 — 해당 답변은 '(ERR...)' (검색/백엔드/터널 확인)", flush=True)

    # Phase 2 — judge (separate model)
    print(f"\nPhase 2 — judge ({args.judge_model})", flush=True)
    results = []
    for i, g in enumerate(gen, 1):
        q, ref, ans, ctx = g["question"], g.get("expected_answer", ""), g["_ans"], g["_ctx"]
        row = {"id": g["id"], "category": g.get("category"), "difficulty": g.get("difficulty"),
               "intent": g.get("gold_intent"), "question": q, "reference": ref, "answer": ans, "context": ctx,
               "sources": g["_srcs"], "doc_hit": g["_doc_hit"], "matched_sources": g["_matched"],
               "guard": g["_guard"], "must_include": g.get("must_include", []),
               "must_not_include": g.get("must_not_include", []), "gold_document": g.get("gold_document"),
               "duration_ms": g["_dur"]}
        for m in rag.METRICS:
            sysp, tmpl, _ = rag.METRIC_CONFIG[m]
            prompt = tmpl.format(question=q[:500], context=ctx[:1200], answer=ans[:500], reference=ref[:300])
            try:
                sc, reason = rag.extract_score(judge_ollama(args.judge_url, args.judge_model, args.think, sysp, prompt))
            except Exception as e:
                sc, reason = -1.0, f"(ERR {str(e)[:80]})"
            row[m], row[m + "_reason"] = sc, reason
        print(f"  judge [{i:3}/{len(data)}] id={g['id']:>4} "
              + " ".join(f"{m.split('_')[0][:4]}={row[m]:.2f}" for m in rag.METRICS), flush=True)
        results.append(row)

    # Per-metric stats: a failed judge call is scored -1 and MUST NOT silently shrink the mean
    # without the operator knowing (reviewer: no silent errors). Track valid-n + failure counts.
    def stat(m):
        vals = [r[m] for r in results if isinstance(r.get(m), (int, float)) and r[m] >= 0]
        return (round(sum(vals) / len(vals), 4) if vals else None), len(vals)

    ragas, valid_n = {}, {}
    for m in rag.METRICS:
        ragas[m], valid_n[m] = stat(m)
    judge_failures = {m: len(results) - valid_n[m] for m in rag.METRICS}
    total_judge_fail = sum(judge_failures.values())

    prec, rec = ragas["context_precision"], ragas["context_recall"]
    kpi = {"Accuracy": ragas["answer_correctness"], "Precision": prec, "Recall": rec,
           "F1": hmean(prec or 0, rec or 0), "Faithfulness": ragas["faithfulness"],
           "AnswerRelevancy": ragas["answer_relevancy"]}
    # Only records with a concrete gold doc are scorable for retrieval recall; sentinel
    # gold_document (e.g. "기타") has no target to retrieve and must not dilute the rate.
    scorable = [r for r in results if qa_scorer.is_retrievable_gold(r.get("gold_document"))]
    doc_recall_rate = round(sum(1 for r in scorable if r["doc_hit"]) / len(scorable), 4) if scorable else None
    guard_viol = sum(1 for r in results if r["guard"] == "VIOLATION")

    print("\n=== KPI ===")
    for k, v in kpi.items():
        bar = ("█" * int((v or 0) * 20) + "░" * (20 - int((v or 0) * 20))) if v is not None else "N/A"
        print(f"  {k:<16} {bar} {v}")
    print(f"\n  doc_recall(string match)={doc_recall_rate}  guard_violations={guard_viol}/{len(results)}")

    # Loud warnings so a shrunken/None KPI is never mistaken for a clean run.
    if total_judge_fail:
        print(f"[WARN] judge 점수 파싱 실패 {total_judge_fail}건(지표별 {judge_failures}) — 해당 점수는 평균에서 "
              f"제외돼 표본이 줄었다. judge 모델/엔드포인트({args.judge_url}) 확인 후 재실행 권장.", flush=True)
    degraded = [m for m in rag.METRICS if valid_n[m] < len(results)]
    if degraded:
        print(f"[WARN] 표본 축소 지표: " + ", ".join(f"{m}({valid_n[m]}/{len(results)})" for m in degraded), flush=True)
    if any(ragas[m] is None for m in rag.METRICS):
        print("[WARN] 유효 점수 0건인 지표 존재 → KPI None. judge가 전혀 채점 못 함(think 설정/모델명/엔드포인트 확인).", flush=True)

    payload = {"judge": f"ollama:{args.judge_model}", "judge_url": args.judge_url, "generator": args.base,
               "generator_model": gen_model, "n": len(data), "ragas": ragas, "kpi": kpi,
               "valid_n": valid_n, "judge_failures": judge_failures, "gen_errors": gen_errors,
               "doc_recall_rate": doc_recall_rate, "guard_violations": guard_viol, "results": results}
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(_REPO, "logs"), exist_ok=True)
    for name in (f"ragas_kpi_{args.tag}_{ts}.json", f"ragas_kpi_{args.tag}_latest.json"):
        with open(os.path.join(_REPO, "logs", name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\nreport -> logs/ragas_kpi_{args.tag}_{ts}.json  (latest -> logs/ragas_kpi_{args.tag}_latest.json)")


if __name__ == "__main__":
    main()
