"""Core QA KPIs — Accuracy / Precision / Recall / F1 / Faithfulness — pure/offline.

The five headline numbers a QA engineer reports for the RAG chatbot, computed
from a scored run (gold dataset + model answers + optional retrieved contexts).
No live LLM/Qdrant: everything here is rule-based on strings you already have,
so it runs in the offline ``pytest -m "not integration"`` lane.

Framing (documented, so the numbers are unambiguous)
----------------------------------------------------
Each item is either **answerable** (a correct answer exists in the KB) or not
(the bot *should refuse*). We treat "the bot committed to a substantive answer"
as the positive prediction, and score it against whether that was the right move:

    gave_answer = 답이 공란도 아니고 거부도 아님(실제로 내용을 답함)
    correct     = is_correct(case)  (정답 사실 포함 & 금지어 없음 / 거부해야 할 땐 거부)

    TP  gave_answer & correct                 (답했고 맞음)
    FP  gave_answer & not correct             (답했는데 틀림 · 거부했어야 하는데 답함)
    FN  not gave_answer & answerable          (답할 수 있었는데 거부/공란)
    TN  not gave_answer & not answerable      (거부해야 할 걸 제대로 거부)

    Accuracy    = (TP+TN)/N            전체 정답률(거부 정답 포함)
    Precision   = TP/(TP+FP)           내놓은 답 중 신뢰할 수 있는 비율
    Recall      = TP/(TP+FN)           답할 수 있는 질문을 실제로 맞힌 비율
    F1          = 2PR/(P+R)
    Faithfulness= 답의 주장 중 회수된 컨텍스트로 뒷받침되는 비율(어휘 근거 proxy)

``Faithfulness`` here is a **lexical proxy** for RAGAS faithfulness (fact/token
support of the answer by the retrieved context). The LLM-judged version lives in
``eval_tools/_ragas_eval.py``; use that when a judge model is available and this
as the fast, deterministic, offline gate.

Retrieval metrics (recall/precision of the gold evidence in the retrieved set)
are reported as a bonus when contexts are present — see ``retrieval_recall`` /
``retrieval_precision`` on the result.

PURE: stdlib + ``eval_tools.kpi.error_analysis`` (also pure) only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .error_analysis import (
    _contains,
    _doc_key,
    _gold_facts,
    _is_refusal,
    _tokens,
    extract_retrieved,
    is_correct,
)
from .error_analysis import join_predictions as join_predictions  # re-export


def gave_answer(answer: str) -> bool:
    """Did the bot commit to a substantive answer (not blank, not a bare refusal)?

    Intuitive string-only helper. Note the *scoring* path uses ``_committed``
    (gold-aware) so a correct answer that merely contains a negation word
    (…휴학이 **불가**능합니다) is NOT mistaken for a refusal — the false-refusal
    trap ``scorer.py`` (D3) warns about.
    """
    return bool((answer or "").strip()) and not _is_refusal(answer)


def _committed(case: dict) -> bool:
    """Did the bot commit to a substantive answer, gold-aware?

    A "punt" (= did NOT commit) is an empty answer, or a refusal that carries
    none of the gold facts. A refusal-worded but fact-bearing answer (e.g.
    "…휴학이 불가능합니다") still counts as committed — avoiding the false-refusal
    bug that would otherwise deflate Recall and desync Accuracy from
    ``error_analysis``.
    """
    answer = case.get("answer", "") or ""
    if not answer.strip():
        return False
    # For unanswerable items, gold facts are meaningless (expected_answer is itself
    # a refusal), so any refusal there is a punt. For answerable items, a refusal
    # that still carries a gold fact is a real (committed) answer.
    facts = _gold_facts(case) if case.get("answerable", True) else []
    if _is_refusal(answer) and not any(_contains(f, answer) for f in facts):
        return False
    return True


def _faithfulness_item(case: dict) -> Optional[float]:
    """Per-item grounding proxy in [0,1], or None if not measurable.

    Prefers gold-fact support: of the gold facts the answer actually asserts,
    how many also appear in the retrieved context. Falls back to answer↔context
    content-token overlap when the answer asserts no gold facts.
    """
    retrieved = case.get("retrieved")
    if not isinstance(retrieved, list):
        retrieved = extract_retrieved(case)
    if not retrieved:
        return None
    answer = case.get("answer", "") or ""
    if not _committed(case):
        return None  # nothing substantive asserted -> not a faithfulness event
    ctx = "  ".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in retrieved)
    asserted = [f for f in _gold_facts(case) if _contains(f, answer)]
    if asserted:
        supported = sum(1 for f in asserted if _contains(f, ctx))
        return supported / len(asserted)
    ans_tokens = _tokens(answer)
    if not ans_tokens:
        return None
    ctx_tokens = _tokens(ctx)
    return len(ans_tokens & ctx_tokens) / len(ans_tokens)


def _retrieval_hit(case: dict) -> Optional[bool]:
    """Was the gold evidence (fact-in-context, or gold doc) in the retrieved set?

    None when there is no retrieval to score (no contexts on the record).
    """
    retrieved = case.get("retrieved")
    if not isinstance(retrieved, list):
        retrieved = extract_retrieved(case)
    if not retrieved:
        return None
    ctx = "  ".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in retrieved)
    facts = _gold_facts(case)
    if facts and any(_contains(f, ctx) for f in facts):
        return True
    gold_key = _doc_key(case.get("gold_document", ""))
    if gold_key:
        docs = [_doc_key(r.get("doc", "")) for r in retrieved if isinstance(r, dict)]
        if any(gold_key == d or gold_key in d or d in gold_key for d in docs if d):
            return True
    return False


@dataclass
class MetricSet:
    n: int
    tp: int
    fp: int
    fn: int
    tn: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    faithfulness: Optional[float] = None       # None => no contexts to judge
    retrieval_recall: Optional[float] = None    # gold evidence present in retrieved set
    retrieval_precision: Optional[float] = None  # answered items whose evidence was retrieved
    n_faithfulness: int = 0                     # items faithfulness was measured over

    def as_dict(self) -> dict:
        return {
            "n": self.n, "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "accuracy": self.accuracy, "precision": self.precision,
            "recall": self.recall, "f1": self.f1, "faithfulness": self.faithfulness,
            "retrieval_recall": self.retrieval_recall,
            "retrieval_precision": self.retrieval_precision,
            "n_faithfulness": self.n_faithfulness,
        }


def _ratio(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def compute(cases: Iterable[dict]) -> MetricSet:
    """Compute the five headline KPIs (+ retrieval bonus) over scored cases."""
    tp = fp = fn = tn = 0
    faith_vals: list[float] = []
    retr_hits = 0
    retr_total = 0            # items with contexts AND that the bot answered
    retr_hits_all = 0
    retr_total_all = 0        # all items with contexts (recall denominator)
    for case in cases:
        correct = is_correct(case)
        answered = _committed(case)
        answerable = case.get("answerable", True)
        if answered and correct:
            tp += 1
        elif answered and not correct:
            fp += 1
        elif (not answered) and answerable:
            fn += 1
        else:  # not answered and not answerable
            tn += 1

        fi = _faithfulness_item(case)
        if fi is not None:
            faith_vals.append(fi)

        hit = _retrieval_hit(case)
        if hit is not None:
            retr_hits_all += int(hit)
            retr_total_all += 1
            if answered:
                retr_hits += int(hit)
                retr_total += 1

    n = tp + fp + fn + tn
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) else 0.0
    return MetricSet(
        n=n, tp=tp, fp=fp, fn=fn, tn=tn,
        accuracy=_ratio(tp + tn, n),
        precision=precision, recall=recall, f1=f1,
        faithfulness=round(sum(faith_vals) / len(faith_vals), 4) if faith_vals else None,
        retrieval_recall=_ratio(retr_hits_all, retr_total_all) if retr_total_all else None,
        retrieval_precision=_ratio(retr_hits, retr_total) if retr_total else None,
        n_faithfulness=len(faith_vals),
    )


def render_markdown(m: MetricSet) -> str:
    def pct(x: Optional[float]) -> str:
        return "N/A" if x is None else f"{x:.1%}"

    lines = [
        "# KPI (Accuracy / Precision / Recall / F1 / Faithfulness)",
        "",
        f"- 문항 수 N = **{m.n}**  (TP {m.tp} · FP {m.fp} · FN {m.fn} · TN {m.tn})",
        "",
        "| 지표 | 값 |",
        "|---|--:|",
        f"| Accuracy | **{m.accuracy:.1%}** |",
        f"| Precision | **{m.precision:.1%}** |",
        f"| Recall | **{m.recall:.1%}** |",
        f"| F1 | **{m.f1:.1%}** |",
        f"| Faithfulness (lexical proxy, n={m.n_faithfulness}) | **{pct(m.faithfulness)}** |",
    ]
    if m.retrieval_recall is not None:
        lines.append(f"| Retrieval recall (근거 회수율) | {pct(m.retrieval_recall)} |")
    if m.retrieval_precision is not None:
        lines.append(f"| Retrieval precision (답변 시 근거 확보율) | {pct(m.retrieval_precision)} |")
    lines.append("")
    return "\n".join(lines)


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="QA KPIs: Accuracy/Precision/Recall/F1/Faithfulness (offline)")
    ap.add_argument("--dataset", required=True, help="gold dataset json (qa_dataset schema)")
    ap.add_argument("--predictions", required=True,
                    help="prediction dump: a list, or {'results': [...]} of {id, answer, retrieved/results}")
    ap.add_argument("--json", action="store_true", help="emit json instead of markdown")
    args = ap.parse_args(argv)

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)
    with open(args.predictions, encoding="utf-8") as f:
        preds = json.load(f)
    if isinstance(preds, dict):
        preds = preds.get("results") or preds.get("predictions") or []

    m = compute(join_predictions(dataset, preds))
    if args.json:
        print(json.dumps(m.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
