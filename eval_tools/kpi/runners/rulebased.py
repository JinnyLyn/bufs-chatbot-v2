"""Rule-based KPI runner — score predictions via scorer.py.

Offline: no network, no Ollama, no Qdrant. Accepts a list of records in any
of the four WS-0a shapes (canonical answer-field resolution via schema.py).
Returns a :class:`~eval_tools.kpi.scorer.ScoreResult`.

Scoring semantics (corrected lineage):
- **contains** (``some``): at least one extracted fact present in the answer.
- **strict** (``full``): all extracted facts present.
- **refusal** (unanswerable only): answer contains a refusal marker.
- **D3 correction**: refusal-word subtraction is NEVER applied to answerable
  items — fact-presence is judged independently of refusal markers.
- **empty-facts fallback**: when ``extract_facts(ground_truth)`` is empty,
  score on Korean/Latin token overlap (``ov >= 0.6`` → full, ``ov >= 0.3`` → some).

This runner is the ``--from-predictions`` scoring stage: it expects records
already loaded from dump files and normalized. For loading / glob expansion,
see the CLI (``eval_tools.kpi.cli``).
"""
from __future__ import annotations

from typing import Iterable

from ..schema import normalize_record
from ..scorer import ScoreResult, score


def run(records: Iterable[dict]) -> ScoreResult:
    """Score prediction records → :class:`~eval_tools.kpi.scorer.ScoreResult`.

    Accepts any WS-0a dump shape (``answer`` / ``model_answer`` / ``prediction``
    are all resolved via :func:`~eval_tools.kpi.schema.normalize_record`).

    Parameters
    ----------
    records:
        Iterable of raw prediction-dump dicts.  Each record must carry at
        minimum ``answerable`` (bool) and either an answer field or no answer
        (unanswerable-only scoring path).

    Returns
    -------
    ScoreResult
        Aggregate ``contains_rate``, ``strict_rate``, ``refusal_rate`` and
        the full per-item ``items`` tuple.
    """
    return score(normalize_record(r) for r in records)
