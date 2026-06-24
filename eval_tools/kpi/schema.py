"""Canonical KPI record schema + the WS-0a 4-shape field-mapping.

This module is the **contract** every other ``eval_tools.kpi`` module builds
to. It is PURE — stdlib only, no ``import config``, no file/network I/O — so it
runs in the default offline ``pytest -m "not integration"`` lane.

Why a mapping at all
--------------------
Several JSON shapes for the same combined88 questions coexist in the repo. They
differ mainly in *where the model's answer lives* (``answer`` vs
``model_answer`` vs ``prediction``) and in which fields are present. The scorer
must read one canonical set of inputs regardless of which shape it is handed.

The four shapes (WS-0a field-mapping table)
-------------------------------------------
Columns are the source shapes; rows are the canonical scorer inputs.

| scorer input | committed test-set            | golden-outputs corpus | --from-predictions dump / | _aggregate_variants
|              | (inputs-only)                 |                       | canonical dump            | logs/combined88_*.json |
|--------------|-------------------------------|-----------------------|---------------------------|------------------------|
| question     | ``question``                  | ``question``          | ``question``              | ``question``           |
| ground_truth | ``ground_truth``              | ``ground_truth``      | ``ground_truth``          | ``ground_truth``       |
| answerable   | ``answerable``                | ``answerable``        | ``answerable``            | ``answerable``         |
| answer       | — (no prediction)             | ``model_answer``      | ``answer``                | ``answer``             |
| latency      | —                             | —                     | ``duration_ms``           | ``duration_ms``        |
| retrieved    | —                             | ``retrieved_docs``    | ``results[]`` (live-only) | n/a                    |

Concrete top-level shapes
-------------------------
1. **Committed test-set (inputs-only)** — ``eval_tools/data/combined88.json``::

       {"meta": {...}, "results": [ {id, question, ground_truth,
                                     intent, difficulty, answerable, gt_source} ]}

   No prediction/latency/retrieval — those are stripped. Scored only after a
   model answer is attached (live run or ``--from-predictions``).

2. **Golden-outputs corpus** — ``tests/kpi/fixtures/*_golden_outputs.json``::

       [ {id, question, model_answer, retrieved_docs, ground_truth,
          answerable} -> {facts, matched_flags, full, some, is_refusal,
                          used_token_fallback} ]

   Uses ``model_answer`` for human readability.

3. **Canonical prediction dump** (the ``--from-predictions`` shape, == what
   ``_aggregate_variants.score()`` already reads)::

       {"results": [ {id, question, ground_truth, answerable,
                      answer, duration_ms, timing, results, tool_calls} ]}

4. **Legacy ``logs/combined88_*.json``** — same as (3): top-level ``results``
   with per-record ``answer`` + ``duration_ms`` (plus ``facts``/``matched``/
   ``verdict`` for answerable records). This is the gate-of-record snapshot
   shape (e.g. ``logs/combined88_new_result.json``).

Normalization rule
------------------
``answer`` | ``model_answer`` | ``prediction`` -> canonical ``answer``;
``ground_truth``, ``answerable``, ``duration_ms`` pass through by name. The
accessors below implement this so the scorer never special-cases a shape.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict

# Answer field aliases, in resolution priority. The canonical dump + legacy
# logs use ``answer``; the golden corpus uses ``model_answer``; older prediction
# dumps use ``prediction``. First present (non-None) key wins.
ANSWER_KEYS: tuple[str, ...] = ("answer", "model_answer", "prediction")

# Inputs-only fields kept in the committed test set (predictions stripped).
INPUT_FIELDS: tuple[str, ...] = (
    "id",
    "question",
    "ground_truth",
    "intent",
    "difficulty",
    "answerable",
    "gt_source",
)


class Record(TypedDict, total=False):
    """Canonical KPI record (post-normalization).

    ``total=False`` because the committed test-set carries no ``answer``/
    ``duration_ms`` until a model answer is attached, and unanswerable records
    carry no fact-bearing ``ground_truth`` worth extracting.
    """

    id: Any
    question: str
    ground_truth: str
    answerable: bool
    answer: str
    duration_ms: Optional[float]
    intent: Optional[str]
    difficulty: Optional[str]
    gt_source: Optional[str]


def canonical_answer(record: dict) -> str:
    """Return the model answer for ``record`` across all known shapes.

    Resolves ``answer`` -> ``model_answer`` -> ``prediction`` (first present,
    non-None). Missing answer -> ``""`` (mirrors ``_aggregate_variants``'s
    ``r.get("answer", "") or ""`` so scoring is parity-safe).
    """
    for key in ANSWER_KEYS:
        val = record.get(key)
        if val is not None:
            return str(val)
    return ""


def canonical_ground_truth(record: dict) -> str:
    """Return the ground-truth text (``""`` if absent)."""
    return str(record.get("ground_truth") or "")


def is_answerable(record: dict) -> bool:
    """Truthy ``answerable`` flag (mirrors ``_aggregate_variants``)."""
    return bool(record.get("answerable"))


def duration_seconds(record: dict) -> Optional[float]:
    """Latency in seconds from ``duration_ms`` (``None`` if absent/non-numeric)."""
    ms = record.get("duration_ms")
    if isinstance(ms, (int, float)):
        return ms / 1000.0
    return None


def normalize_record(record: dict) -> Record:
    """Map any of the four source shapes to a canonical :class:`Record`.

    The canonical ``answer`` is resolved across ``answer``/``model_answer``/
    ``prediction``; all other fields pass through by name. Absent optional
    fields are simply omitted (``Record`` is ``total=False``).
    """
    out: Record = {}
    for field in ("id", "question", "intent", "difficulty", "gt_source"):
        if field in record:
            out[field] = record[field]  # type: ignore[literal-required]
    out["ground_truth"] = canonical_ground_truth(record)
    out["answerable"] = is_answerable(record)
    if any(k in record for k in ANSWER_KEYS):
        out["answer"] = canonical_answer(record)
    dur = record.get("duration_ms")
    if isinstance(dur, (int, float)):
        out["duration_ms"] = dur
    return out
