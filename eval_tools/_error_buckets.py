"""7-bucket error analysis for a RAGAS+KPI run (consumes ``_ragas_kpi.py`` output).

Routes each WRONG answer into one of the buckets the QA role tracks, using RAGAS signals +
retrieval facts (sources, doc_recall, must_not_include guard):

  검색 실패 / 문서 없음 / Embedding 문제 / Chunk 문제   (retrieval side)
  LLM Hallucination / Prompt 실패                          (generation side)
  질문 애매함                                              (question/dataset side)

Decision tree (auto first-pass — every row carries signals + a confidence flag so a human
can confirm/override):

  correctness >= THRESH:
     guard CLEAN     -> 정답
     guard VIOLATION -> 정답(guard 오탐)   # generic distractor collided with a correct substring
  correctness <  THRESH  (genuine error):
     guard VIOLATION                        -> Prompt 실패
     context_recall < 0.5 (evidence absent) -> retrieval side:
         gold doc not mappable to KB        -> 문서 없음
         gold doc retrieved, fact missing   -> Chunk 문제
         gold doc in KB but not retrieved   -> Embedding 문제
     else (evidence present, answer wrong)  -> generation side:
         faithfulness < 0.6                 -> LLM Hallucination
         answer_relevancy < 0.4             -> 질문 애매함
         otherwise                          -> Prompt 실패

Run::  python eval_tools/_error_buckets.py --in logs/ragas_kpi_factual100_latest.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

THRESH = 0.7  # answer_correctness >= THRESH counts as correct (RAGAS judge)
BUCKET_ORDER = ["정답", "정답(guard 오탐)", "검색 실패", "문서 없음", "Embedding 문제", "Chunk 문제",
                "LLM Hallucination", "Prompt 실패", "질문 애매함", "판정불가"]
CORRECT_BUCKETS = ("정답", "정답(guard 오탐)")


def _norm(s: str) -> str:
    return re.sub(r"[\s\-\(\)\[\]\.·/,]+", "", (s or "").lower())


def _toks(s: str) -> list[str]:
    return [t for t in re.split(r"[\s\-\(\)\[\]\.·/,]+", (s or "").lower()) if t]


def kb_docs(parent_store: str) -> list[str]:
    if not os.path.isdir(parent_store):
        return []
    return sorted({re.sub(r"_parent_\d+\.json$", "", f) for f in os.listdir(parent_store)})


def map_gold_to_kb(gold: str, kb: list[str]) -> str | None:
    gt = _toks(gold)
    if not gt:
        return None
    for d in kb:
        dn = _norm(d)
        if all(_norm(w) in dn for w in gt):
            return d
    return None


def doc_hit_fuzzy(gold: str, sources: list[str], kb: list[str]) -> bool:
    kbdoc = map_gold_to_kb(gold, kb)
    targets = {_norm(gold)} | ({_norm(kbdoc)} if kbdoc else set())
    for s in sources or []:
        ns = _norm(s)
        if any(t and (t in ns or ns in t) for t in targets):
            return True
    return False


def classify(r: dict, kb: list[str]) -> tuple[str, str, str]:
    corr = r.get("answer_correctness", -1)
    cr = r.get("context_recall", -1)
    f = r.get("faithfulness", -1)
    ar = r.get("answer_relevancy", -1)
    guard = r.get("guard", "CLEAN")

    if corr is None or corr < 0:
        return "판정불가", "judge 점수 없음", "low"
    if corr >= THRESH:
        if guard == "VIOLATION":
            return "정답(guard 오탐)", f"correctness={corr:.2f} 정답이나 금지어 substring 검출 — distractor 재검토", "high"
        return "정답", f"correctness={corr:.2f}", "high"

    if guard == "VIOLATION":
        return "Prompt 실패", "금지어 유출(오답 + guard VIOLATION)", "high"

    kbdoc = map_gold_to_kb(r.get("gold_document"), kb)
    hit = doc_hit_fuzzy(r.get("gold_document"), r.get("sources"), kb)
    if cr is not None and 0 <= cr < 0.5:
        if kbdoc is None:
            return "문서 없음", f"gold_document '{r.get('gold_document')}' KB 매핑 실패(공지 카테고리 가능) cr={cr:.2f}", "low"
        if hit:
            return "Chunk 문제", f"gold 문서 검색됨({kbdoc}) 그러나 근거 청크 누락 cr={cr:.2f}", "medium"
        return "Embedding 문제", f"gold 문서 KB에 있음({kbdoc}) 그러나 미검색 cr={cr:.2f}", "medium"

    if f is not None and 0 <= f < 0.6:
        return "LLM Hallucination", f"근거 있음(cr={cr:.2f}) but faithfulness={f:.2f}", "high"
    if ar is not None and 0 <= ar < 0.4:
        return "질문 애매함", f"answer_relevancy={ar:.2f} 낮음(질문 다의/모호 의심)", "low"
    return "Prompt 실패", f"근거 있음(cr={cr:.2f},faith={f:.2f}) but correctness={corr:.2f} — 추출/형식 실패", "medium"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="ragas_kpi results json")
    ap.add_argument("--parent-store", default=os.path.join(_REPO, "parent_store"),
                    help="KB parent_store dir (for 문서없음/Embedding/Chunk 구분)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("results")
    if not rows:
        raise SystemExit(f"[error] {args.inp}: 'results' 키가 없거나 비어 있습니다 — _ragas_kpi 출력 파일인지 확인.")
    kb = kb_docs(args.parent_store)
    if not kb:
        print(f"[warn] parent_store 비어있음/없음 ({args.parent_store}) — 문서없음/Embedding 판정 신뢰도 하락", flush=True)

    out_rows, cnt = [], Counter()
    for r in rows:
        b, reason, conf = classify(r, kb)
        cnt[b] += 1
        out_rows.append({"id": r["id"], "category": r.get("category"), "difficulty": r.get("difficulty"),
                         "question": r["question"], "bucket": b, "confidence": conf, "reason": reason,
                         "correctness": r.get("answer_correctness"), "faithfulness": r.get("faithfulness"),
                         "context_recall": r.get("context_recall"), "context_precision": r.get("context_precision"),
                         "answer_relevancy": r.get("answer_relevancy"), "doc_hit": r.get("doc_hit"),
                         "guard": r.get("guard")})

    n = len(rows)
    n_correct = sum(cnt[b] for b in CORRECT_BUCKETS)
    n_wrong = n - n_correct - cnt["판정불가"]
    print(f"=== 오답 분석 (7-bucket) | n={n} | 정답={n_correct} 오답={n_wrong} 판정불가={cnt['판정불가']} ===\n")
    print(f"{'버킷':<18}{'건수':>5}{'전체%':>7}{'오답중%':>8}")
    for b in BUCKET_ORDER:
        if cnt[b] == 0 and b != "정답":
            continue
        ofw = f"{cnt[b]/n_wrong*100:6.0f}%" if (n_wrong and b not in CORRECT_BUCKETS and b != "판정불가") else "     -"
        print(f"  {b:<16}{cnt[b]:>5}{cnt[b]/n*100:6.0f}%{ofw:>8}")

    print("\n=== 오답 상세 (검토용 · ●high ◐med ○low) ===")
    cf = {"high": "●", "medium": "◐", "low": "○"}
    for r in sorted([r for r in out_rows if r["bucket"] not in CORRECT_BUCKETS], key=lambda r: (r["bucket"], r["id"])):
        print(f"  id={r['id']:>4} [{r['bucket']}] {cf.get(r['confidence'],'?')} "
              f"corr={r['correctness']} f={r['faithfulness']} cr={r['context_recall']} | {r['question'][:32]}")
        print(f"        └ {r['reason']}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join(_REPO, "logs", f"error_buckets_{ts}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"n": n, "correct": n_correct, "wrong": n_wrong, "buckets": dict(cnt), "rows": out_rows},
                  fh, ensure_ascii=False, indent=2)
    print(f"\nreport -> {os.path.relpath(out, _REPO)}")


if __name__ == "__main__":
    main()
