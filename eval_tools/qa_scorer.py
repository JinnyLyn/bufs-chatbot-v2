"""Canonical QA-dataset loader + rule-based scorer for the BUFS chatbot eval.

The golden dataset lives **in-repo** at ``eval_tools/datasets/qa_dataset.json`` so eval
runs are reproducible after a fresh clone (the old combined88 harness read an absolute
path into a sibling ``bufs-chatbot`` repo that does not exist on other machines).

Dataset schema (one object per question)::

    id, question, gold_intent, gold_document, expected_answer,
    must_include[], must_not_include[], difficulty, category
    (optional/reserved: gold_chunk_id — for future chunk-level retrieval recall)

Rule-based scoring enforces **``must_not_include`` only** (a hard "forbidden phrase"
guard). ``must_include`` is intentionally NOT scored here: in this dataset the tokens are
loose semantic keywords that the terse ``expected_answer`` itself often does not contain
verbatim, so an exact-string rule would mis-fail correct answers. Answer *correctness* is
judged against ``expected_answer`` by RAGAS / LLM-judge (``_ragas_eval.py``) instead.

There is no refusal heuristic, so the historical false-refusal bug (words like
"불가"/"없습니다" mis-scored as a refusal) cannot occur here.

Pure functions only — no network, no backend. Importable from the CLI runner
(``_eval_qa100.py``) and from tests (``import qa_scorer`` via pythonpath=["eval_tools"]).
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any

# In-repo canonical dataset path, derived from this file's location (worktree-correct).
DATASET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets", "qa_dataset.json")

REQUIRED_FIELDS = (
    "id", "question", "gold_intent", "gold_document",
    "expected_answer", "must_include", "must_not_include", "difficulty", "category",
)
# Reserved/optional. `gold_chunk_id` is a placeholder for future chunk-level retrieval
# recall; it is intentionally empty in every record today (no chunk-level ground truth
# yet), so the loader does NOT require it. Populate it when chunk-recall eval lands.
OPTIONAL_FIELDS = ("gold_chunk_id",)


def load_dataset(path: str | None = None) -> list[dict[str, Any]]:
    """Load and validate the golden dataset. Raises ValueError on schema breaks."""
    path = path or DATASET_PATH
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"dataset must be a non-empty list: {path}")
    ids = set()
    for i, rec in enumerate(data):
        missing = [k for k in REQUIRED_FIELDS if k not in rec]
        if missing:
            raise ValueError(f"record #{i} (id={rec.get('id')}) missing fields: {missing}")
        if not isinstance(rec["must_include"], list) or not isinstance(rec["must_not_include"], list):
            raise ValueError(f"record id={rec['id']}: must_include/must_not_include must be lists")
        if rec["id"] in ids:
            raise ValueError(f"duplicate id: {rec['id']}")
        ids.add(rec["id"])
    return data


def _norm(s: str) -> str:
    """Whitespace-insensitive form so '학부(과) 사무실' matches '학부(과)사무실'."""
    return re.sub(r"\s+", "", s or "")


def _doc_key(name: str) -> str:
    """Normalize a doc title / source filename for fuzzy gold_document matching."""
    base = os.path.basename(name or "")
    base = re.sub(r"\.(md|markdown|pdf|txt|docx?)$", "", base, flags=re.I)
    return re.sub(r"[\s_().\-]+", "", base)


def contains(token: str, answer: str) -> bool:
    """Whitespace-insensitive substring test (forbidden-phrase / exact match)."""
    return _norm(token) in _norm(answer)


def tokens_present(token: str, text: str) -> bool:
    """Order-independent keyword match: are ALL words of `token` present in `text`?

    Splits on whitespace, middot, and slash. Diagnostic-only (used by attribution
    tooling) — NOT used by score_record, which delegates correctness to the LLM judge.
    """
    t = _norm(text)
    return all(_norm(w) in t for w in re.split(r"[\s·/]+", token or "") if w)


def score_record(rec: dict[str, Any], answer: str) -> dict[str, Any]:
    """Rule-based guard for one answer. Enforces ``must_not_include`` only.

    ``must_include`` is NOT scored here (these tokens are loose keywords the terse gold
    answer often lacks verbatim — see module docstring). Answer correctness is judged
    against ``expected_answer`` by RAGAS / LLM-judge separately.

    Verdict:
      - VIOLATION : a must_not_include token leaked into the answer (hard fail)
      - CLEAN     : no forbidden token present
    """
    must_not = rec.get("must_not_include") or []
    violations = [t for t in must_not if contains(t, answer)]
    return {
        "verdict": "VIOLATION" if violations else "CLEAN",
        "clean": not violations,
        "violations": violations,
    }


def intent_match(gold_intent: str, pred_intent: str | None) -> bool:
    """Soft intent match: normalized equality or containment either direction."""
    if not gold_intent or not pred_intent:
        return False
    g, p = _norm(gold_intent), _norm(pred_intent)
    return g == p or g in p or p in g


# gold_document sentinels that denote "no single KB document is the retrieval target"
# (category-less questions whose answer defers to an external notice). These are excluded
# from retrieval-recall scoring so they are not counted as guaranteed misses.
NON_RETRIEVABLE_GOLD = {"기타"}


def is_retrievable_gold(gold_document: str) -> bool:
    """True if gold_document names a concrete KB doc we can score retrieval against.

    Empty or sentinel (e.g. ``기타``) values mean there is no single target document,
    so retrieval recall is undefined for that record and it must not dilute the metric.
    """
    g = (gold_document or "").strip()
    return bool(g) and g not in NON_RETRIEVABLE_GOLD


def doc_recall(gold_document: str, sources: list[str] | None) -> dict[str, Any]:
    """Heuristic retrieval recall: did any retrieved source match gold_document?

    Titles vs filenames differ (spaces/underscores/extension), so match on a stripped
    key with two-way containment. ``matched_sources`` is kept for manual audit.
    """
    sources = sources or []
    gk = _doc_key(gold_document)
    matched = [s for s in sources if gk and (gk in _doc_key(s) or _doc_key(s) in gk)]
    return {"hit": bool(matched), "matched_sources": matched}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-record results into KPI buckets (overall + by category/difficulty).

    Rule-layer headline is the ``must_not_include`` guard (clean_rate / violation_rate);
    answer correctness is reported separately by the RAGAS/LLM-judge harness.
    """
    n = len(results)
    if n == 0:
        return {"n": 0}

    def rate(sel) -> float:
        return round(sum(1 for r in results if sel(r)) / n, 4)

    by: dict[str, dict[str, list]] = {"category": defaultdict(list), "difficulty": defaultdict(list)}
    for r in results:
        by["category"][r.get("category", "?")].append(r)
        by["difficulty"][r.get("difficulty", "?")].append(r)

    def group_rates(group: dict[str, list]) -> dict[str, Any]:
        out = {}
        for key, rows in sorted(group.items()):
            m = len(rows)
            out[key] = {
                "n": m,
                "clean_rate": round(sum(1 for r in rows if r.get("clean")) / m, 4),
                "violation_rate": round(sum(1 for r in rows if r.get("verdict") == "VIOLATION") / m, 4),
            }
        return out

    durations = [r["duration_ms"] for r in results if r.get("duration_ms")]
    has_intent = [r for r in results if r.get("intent_evaluated")]
    has_doc = [r for r in results if r.get("doc_recall_evaluated")]

    summary = {
        "n": n,
        "clean": sum(1 for r in results if r.get("clean")),
        "clean_rate": rate(lambda r: r.get("clean")),
        "violation_rate": rate(lambda r: r.get("verdict") == "VIOLATION"),
        "by_category": group_rates(by["category"]),
        "by_difficulty": group_rates(by["difficulty"]),
    }
    if has_intent:
        summary["intent_accuracy"] = round(
            sum(1 for r in has_intent if r.get("intent_correct")) / len(has_intent), 4
        )
        summary["intent_evaluated"] = len(has_intent)
    if has_doc:
        summary["retrieval_recall"] = round(
            sum(1 for r in has_doc if r.get("doc_hit")) / len(has_doc), 4
        )
        summary["retrieval_evaluated"] = len(has_doc)
    if durations:
        durations.sort()
        summary["latency_ms"] = {
            "avg": int(sum(durations) / len(durations)),
            "p50": durations[len(durations) // 2],
            "max": durations[-1],
        }
    return summary
