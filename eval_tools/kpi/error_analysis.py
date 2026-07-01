"""7-category **오답(wrong-answer) triage** for the BUFS RAG chatbot — pure/offline.

Given a scored run (each item = the gold record + the model answer + the
retrieved contexts), this buckets every *wrong* answer into one of seven failure
classes so a 1000-question run's "200 오답" can be split the way a QA engineer
would by hand:

    검색 실패        RETRIEVAL_FAIL   근거는 있는데 top-k에 안 뜸 (recall/ranking miss)
    문서 없음        NO_DOCUMENT      아예 검색 결과가 0건 (KB에 근거 자체가 없음)
    Chunk 문제       CHUNK            맞는 문서는 떴는데 그 청크에 정답 문장이 안 잘림
    Embedding 문제   EMBEDDING        질의와 동떨어진(어휘 겹침 낮은) 이웃만 회수됨
    질문 애매함      AMBIGUOUS        너무 짧고 모호해 검색이 사방으로 흩어짐
    Prompt 실패      PROMPT           근거는 회수됐는데 모델이 거부/공란 (지침·포맷 실패)
    LLM Hallucination HALLUCINATION   근거가 있는데(혹은 없는데) 엉뚱한 답을 지어냄

This is **heuristic triage, not ground truth** — the same spirit as
``_answer_analysis.py`` (검색실패 vs 생성실패), extended to 7 buckets. It gives a
first-pass distribution a human then spot-checks; the ``reason`` on each result
records exactly which signals fired so a reviewer can overrule it.

PURE: stdlib only (no ``config``/``project.*``/network/file-I/O at import).
Runs in the default offline ``pytest -m "not integration"`` lane.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ── Category registry: code -> (Korean label). Order = report/display order. ──
ERROR_CATEGORIES: dict[str, str] = {
    "RETRIEVAL_FAIL": "검색 실패",
    "NO_DOCUMENT": "문서 없음",
    "CHUNK": "Chunk 문제",
    "EMBEDDING": "Embedding 문제",
    "AMBIGUOUS": "질문 애매함",
    "PROMPT": "Prompt 실패",
    "HALLUCINATION": "LLM Hallucination",
}
CORRECT = "CORRECT"  # sentinel for items that are not wrong

# Refusal markers (shared spirit with scorer.REFUSAL) — a "can't find it" signal.
_REFUSAL: tuple[str, ...] = (
    "없습니다", "없음", "불가", "확인할 수 없", "찾을 수 없", "포함되어 있지 않",
    "직접 확인", "명시되어 있지 않", "알 수 없", "제공되지 않", "찾지 못", "해당 정보",
)
_TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")

# Tunable thresholds (kept as module constants so tests/docs can reference them).
AMBIGUOUS_MAX_QTOKENS = 2      # <= this many query tokens => "terse"
AMBIGUOUS_MIN_DISPERSION = 3   # >= this many distinct retrieved docs => "scattered"
EMBEDDING_MAX_OVERLAP = 0.34   # query↔context lexical overlap below this => embedding-off


# ───────────────────────── normalization helpers ─────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _tokens(s: str) -> set[str]:
    return set(_TOKEN.findall(s or ""))


def _doc_key(name: str) -> str:
    """Normalize a doc title / source filename for fuzzy gold_document matching."""
    base = re.sub(r"^.*[\\/]", "", name or "")
    base = re.sub(r"\.(md|markdown|pdf|txt|docx?|json)$", "", base, flags=re.I)
    return re.sub(r"[\s_().\-]+", "", base)


def _contains(token: str, text: str) -> bool:
    return bool(token) and _norm(token) in _norm(text)


def _is_refusal(answer: str) -> bool:
    return any(m in (answer or "") for m in _REFUSAL)


# ───────────────────────── retrieved-context extraction ─────────────────────────
_TEXT_KEYS = ("text", "content", "page_content", "chunk", "document", "snippet")
_DOC_KEYS = ("doc", "source", "document", "title", "file", "filename", "gold_document")
_SCORE_KEYS = ("score", "similarity", "distance", "relevance")


def _ctx_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in _TEXT_KEYS:
            v = item.get(k)
            if isinstance(v, str) and v:
                return v
        meta = item.get("metadata")
        if isinstance(meta, dict):
            for k in _TEXT_KEYS:
                if isinstance(meta.get(k), str):
                    return meta[k]
    return ""


def _ctx_doc(item: Any) -> str:
    if isinstance(item, dict):
        for k in _DOC_KEYS:
            v = item.get(k)
            if isinstance(v, str) and v:
                return v
        meta = item.get("metadata")
        if isinstance(meta, dict):
            for k in _DOC_KEYS:
                if isinstance(meta.get(k), str):
                    return meta[k]
    return ""


def extract_retrieved(record: dict) -> list[dict]:
    """Normalize a record's retrieved contexts to ``[{text, doc, score}, …]``.

    Reads whichever container the dump uses (``retrieved`` / ``results`` /
    ``retrieved_docs`` / ``contexts``); tolerates str items and nested metadata.
    """
    for key in ("retrieved", "results", "retrieved_docs", "contexts", "context"):
        raw = record.get(key)
        if isinstance(raw, list):
            out = []
            for it in raw:
                score = None
                if isinstance(it, dict):
                    for k in _SCORE_KEYS:
                        if isinstance(it.get(k), (int, float)):
                            score = float(it[k])
                            break
                out.append({"text": _ctx_text(it), "doc": _ctx_doc(it), "score": score})
            return out
    return []


# ───────────────────────── correctness fallback ─────────────────────────
def is_correct(case: dict) -> bool:
    """Rule-based correctness when the caller didn't pre-score the item.

    - ``correct`` field, if present, wins (use your KPI scorer's verdict).
    - unanswerable: correct iff the answer refuses.
    - answerable: correct iff no ``must_not_include`` token appears AND
      (all ``must_include`` tokens appear, or — when none given — the answer
      shares >=60% of ``expected_answer`` content tokens and is non-refusal).
    """
    if isinstance(case.get("correct"), bool):
        return case["correct"]
    answer = case.get("answer", "") or ""
    answerable = case.get("answerable", True)
    if not answerable:
        return _is_refusal(answer)
    for bad in case.get("must_not_include") or []:
        if _contains(bad, answer):
            return False
    inc = case.get("must_include") or []
    if inc:
        return all(_contains(tok, answer) for tok in inc)
    # No gold tokens: fall back to expected_answer content-token overlap.
    gold = _tokens(case.get("expected_answer", ""))
    if not gold:
        return bool(answer.strip()) and not _is_refusal(answer)
    hit = sum(1 for t in gold if t in _tokens(answer))
    return (hit / len(gold)) >= 0.6 and not _is_refusal(answer)


# ───────────────────────── the classifier ─────────────────────────
@dataclass
class ErrorClass:
    code: str                       # ERROR_CATEGORIES key, or CORRECT
    label: str                      # Korean label, or "정답"
    reason: str = ""                # which signals fired (audit trail)

    @property
    def is_error(self) -> bool:
        return self.code != CORRECT


def _gold_facts(case: dict) -> list[str]:
    inc = [t for t in (case.get("must_include") or []) if t]
    if inc:
        return inc
    # derive rough facts from expected_answer (content tokens) when none given
    return sorted(_tokens(case.get("expected_answer", "")))


def classify_error(case: dict) -> ErrorClass:
    """Bucket a single scored case. Returns CORRECT for right answers.

    ``case`` = gold record fields (question, must_include, must_not_include,
    expected_answer, gold_document, answerable) + run outputs (answer, retrieved
    or results/…). Optional pre-scored ``correct`` bypasses the fallback scorer.
    """
    if is_correct(case):
        return ErrorClass(CORRECT, "정답")

    answer = case.get("answer", "") or ""
    answerable = case.get("answerable", True)
    retrieved = case.get("retrieved")
    if not isinstance(retrieved, list):
        retrieved = extract_retrieved(case)
    n_ret = len(retrieved)

    # ── unanswerable but the bot answered instead of refusing ──────────────
    if not answerable:
        if _is_refusal(answer):  # refused -> would be correct; defensive
            return ErrorClass(CORRECT, "정답")
        return ErrorClass(
            "HALLUCINATION", ERROR_CATEGORIES["HALLUCINATION"],
            "unanswerable 질문인데 거부하지 않고 답을 생성함",
        )

    facts = _gold_facts(case)
    ctx_join = "  ".join(_ctx_text(r) for r in retrieved)
    ctx_docs = [_doc_key(_ctx_doc(r)) for r in retrieved if _ctx_doc(r)]
    gold_key = _doc_key(case.get("gold_document", ""))
    gold_doc_hit = bool(gold_key) and any(
        gold_key == d or gold_key in d or d in gold_key for d in ctx_docs
    )
    evidence_in_ctx = any(_contains(f, ctx_join) for f in facts) if facts else False

    # 1) nothing came back at all -> 문서 없음
    if n_ret == 0:
        return ErrorClass("NO_DOCUMENT", ERROR_CATEGORIES["NO_DOCUMENT"],
                          "검색 결과 0건 — 근거 문서 자체가 회수되지 않음")

    # 2) the gold fact WAS in the retrieved context -> generation-side failure
    if evidence_in_ctx:
        if not answer.strip() or _is_refusal(answer):
            return ErrorClass("PROMPT", ERROR_CATEGORIES["PROMPT"],
                              "근거가 컨텍스트에 있는데 답이 거부/공란 — 프롬프트·포맷 실패")
        return ErrorClass("HALLUCINATION", ERROR_CATEGORIES["HALLUCINATION"],
                          "근거가 컨텍스트에 있는데 다른 내용을 답함 — 환각/미사용")

    # 3) right document surfaced but the fact isn't in the surfaced chunk -> Chunk
    if gold_doc_hit:
        return ErrorClass("CHUNK", ERROR_CATEGORIES["CHUNK"],
                          "정답 문서는 회수됐으나 해당 청크에 정답 문장이 없음 — 청킹 문제")

    # 4) terse query + scattered retrieval -> 질문 애매함
    q_tokens = _tokens(case.get("question", ""))
    distinct_docs = len(set(ctx_docs))
    if len(q_tokens) <= AMBIGUOUS_MAX_QTOKENS and distinct_docs >= AMBIGUOUS_MIN_DISPERSION:
        return ErrorClass("AMBIGUOUS", ERROR_CATEGORIES["AMBIGUOUS"],
                          f"질의 토큰 {len(q_tokens)}개로 짧고 검색이 {distinct_docs}개 문서로 흩어짐")

    # 5) retrieved neighbors barely share query vocabulary -> Embedding 문제
    ctx_tokens = _tokens(ctx_join)
    overlap = (len(q_tokens & ctx_tokens) / len(q_tokens)) if q_tokens else 0.0
    if overlap < EMBEDDING_MAX_OVERLAP:
        return ErrorClass("EMBEDDING", ERROR_CATEGORIES["EMBEDDING"],
                          f"회수 청크의 질의 어휘 겹침 {overlap:.2f} < {EMBEDDING_MAX_OVERLAP} — 임베딩 이웃 어긋남")

    # 6) otherwise: relevant-looking chunks came back but missed the gold doc/fact
    return ErrorClass("RETRIEVAL_FAIL", ERROR_CATEGORIES["RETRIEVAL_FAIL"],
                      "질의와 겹치는 청크는 회수됐으나 정답 근거가 top-k에 없음 — 검색 recall/랭킹 실패")


# ───────────────────────── aggregate report ─────────────────────────
@dataclass
class AnalysisReport:
    total: int
    correct: int
    wrong: int
    distribution: dict[str, int]                 # code -> count (wrong only)
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)  # gold category -> {code: n}
    items: list[dict] = field(default_factory=list)  # per-item {id, question, code, label, reason}

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 0.0


def analyze(cases: Iterable[dict]) -> AnalysisReport:
    """Classify every case and aggregate the wrong-answer distribution."""
    dist: Counter[str] = Counter()
    by_cat: dict[str, Counter[str]] = {}
    items: list[dict] = []
    total = correct = 0
    for case in cases:
        total += 1
        ec = classify_error(case)
        if not ec.is_error:
            correct += 1
            continue
        dist[ec.code] += 1
        cat = case.get("category") or "?"
        by_cat.setdefault(cat, Counter())[ec.code] += 1
        items.append({
            "id": case.get("id"),
            "question": case.get("question", ""),
            "code": ec.code,
            "label": ec.label,
            "reason": ec.reason,
        })
    ordered = {c: dist[c] for c in ERROR_CATEGORIES if dist[c]}
    return AnalysisReport(
        total=total,
        correct=correct,
        wrong=total - correct,
        distribution=ordered,
        by_category={k: dict(v) for k, v in by_cat.items()},
        items=items,
    )


def join_predictions(dataset: list[dict], predictions: list[dict]) -> list[dict]:
    """Left-join gold ``dataset`` with a ``predictions`` dump on ``id``.

    Each output case carries gold fields + the prediction's ``answer`` and
    retrieved contexts. Predictions missing an ``id`` match are treated as empty
    (no answer / no retrieval), so a dropped question still scores as wrong.
    """
    pred_by_id = {p.get("id"): p for p in predictions if p.get("id") is not None}
    cases = []
    for g in dataset:
        p = pred_by_id.get(g.get("id"), {})
        case = dict(g)
        case["answer"] = p.get("answer") or p.get("model_answer") or p.get("prediction") or ""
        case["retrieved"] = extract_retrieved(p)
        if isinstance(p.get("correct"), bool):
            case["correct"] = p["correct"]
        cases.append(case)
    return cases


def render_markdown(report: AnalysisReport) -> str:
    """Human-readable distribution table (the '200 오답 분류' view)."""
    lines = [
        "# 오답 분석 (Error Analysis)",
        "",
        f"- 총 문항: **{report.total}**",
        f"- 정답: **{report.correct}**  /  오답: **{report.wrong}**  "
        f"(Accuracy **{report.accuracy:.1%}**)",
        "",
        "## 오답 분류 분포",
        "",
        "| 분류 | 코드 | 개수 | 오답 중 비율 |",
        "|---|---|--:|--:|",
    ]
    wrong = report.wrong or 1
    for code, label in ERROR_CATEGORIES.items():
        n = report.distribution.get(code, 0)
        lines.append(f"| {label} | `{code}` | {n} | {n / wrong:.1%} |")
    lines.append("")
    if report.by_category:
        lines += ["## 카테고리별 오답", "", "| 카테고리 | 오답 | 상위 분류 |", "|---|--:|---|"]
        for cat, d in sorted(report.by_category.items(), key=lambda kv: -sum(kv[1].values())):
            top = ", ".join(
                f"{ERROR_CATEGORIES.get(c, c)} {n}"
                for c, n in sorted(d.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"| {cat} | {sum(d.values())} | {top} |")
        lines.append("")
    return "\n".join(lines)


# ───────────────────────── CLI ─────────────────────────
def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="7-category 오답 분석 (pure/offline)")
    ap.add_argument("--dataset", required=True, help="gold dataset json (qa_dataset schema)")
    ap.add_argument("--predictions", required=True,
                    help="prediction dump: a list, or {'results': [...]} of {id, answer, retrieved/results}")
    ap.add_argument("--json", action="store_true", help="emit machine-readable json instead of markdown")
    args = ap.parse_args(argv)

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)
    with open(args.predictions, encoding="utf-8") as f:
        preds = json.load(f)
    if isinstance(preds, dict):
        preds = preds.get("results") or preds.get("predictions") or []

    report = analyze(join_predictions(dataset, preds))
    if args.json:
        print(json.dumps({
            "total": report.total, "correct": report.correct, "wrong": report.wrong,
            "accuracy": report.accuracy, "distribution": report.distribution,
            "by_category": report.by_category, "items": report.items,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
