"""eval_tools.kpi.runners — KPI measurement runners + per-run metric builder.

Sub-modules
-----------
rulebased       Offline scorer over prediction records → ScoreResult.
latency         p50/p90/p95/max + per-node timing from dump records → LatencyResult.
retrieval       Live Qdrant recall@k/mrr. Raises RetrievalSkipError on dump path.
ragas           Opt-in LLM judge. Returns RagasSentinel when no judge configured.
backend_client  POST /api/session + GET /api/chat/stream SSE → DoneEvent.

Per-run metric contract
-----------------------
:func:`build_run_metrics` combines runner outputs into the dict the gate
expects.  Exact keys (gate-of-record contract, MUST NOT drift):

    contains_rate      float   answerable contains rate
    strict_rate        float   answerable strict (all-facts) rate
    refusal_rate       float   unanswerable correct-refusal rate
    latency_p95_s      float   p95 end-to-end latency in **seconds**
    latency_max_s      float   max end-to-end latency in **seconds**
    total_count        int     total questions attempted this run
    excluded_count     int     questions excluded (error/timeout during live run)
    measurement_error  None|str  None = run succeeded; str = why it failed
                                (unreachable backend/5xx → gate exits 2, NOT 1)
    ragas              None|dict  None when SKIPPED; {metric: float} when scored
    retrieval          None|dict  None when SKIPPED; {recall, mrr, coverage, ...}
"""
from __future__ import annotations

from typing import Optional, Union

from ..scorer import ScoreResult
from .latency import LatencyResult
from .ragas import RagasResult, RagasSentinel
from .retrieval import RetrievalResult


def build_run_metrics(
    *,
    score: Optional[ScoreResult] = None,
    latency: Optional[LatencyResult] = None,
    ragas: Optional[Union[RagasResult, RagasSentinel]] = None,
    retrieval: Optional[RetrievalResult] = None,
    total_count: int = 0,
    excluded_count: int = 0,
    measurement_error: Optional[str] = None,
) -> dict:
    """Combine runner outputs into the per-run metric dict the gate consumes.

    The gate accepts a **list** of these dicts (one per dump/live-run);
    call this once per run and append to the list before calling the gate.

    Parameters
    ----------
    score:
        :class:`~eval_tools.kpi.scorer.ScoreResult` from :mod:`rulebased`.
        ``None`` → all accuracy rates are ``0.0`` (unusual; include a
        ``measurement_error`` reason in that case).
    latency:
        :class:`~eval_tools.kpi.runners.latency.LatencyResult`.
        Already in seconds — no conversion needed.
        ``None`` → both latency fields are ``0.0``.
    ragas:
        :class:`~eval_tools.kpi.runners.ragas.RagasResult` on success, or
        :class:`~eval_tools.kpi.runners.ragas.RagasSentinel` when skipped.
        ``None`` or sentinel → ``ragas`` key is ``None`` in output (SKIPPED).
    retrieval:
        :class:`~eval_tools.kpi.runners.retrieval.RetrievalResult` on
        success.  ``None`` → ``retrieval`` key is ``None`` (SKIPPED).
    total_count:
        Total questions attempted this run (denominator for error budget).
        For ``--from-predictions``, use ``len(records)``.
    excluded_count:
        Questions excluded from scoring due to error/timeout during a live
        run.  Gate raises ERROR (exit 2) when
        ``excluded_count / total_count > error_budget (5%)``.
    measurement_error:
        ``None`` on success.  A short descriptive string when the run could
        not produce a trustworthy number (e.g. ``"backend unreachable"``).
        The gate converts a non-None value to ERROR (exit 2), never NO-GO.

    Returns
    -------
    dict
        Per-run metric dict with the exact keys listed in the module docstring.
    """
    # RAGAS: None when sentinel (is_na=True) or not run at all.
    ragas_out: Optional[dict] = None
    if ragas is not None and not getattr(ragas, "is_na", True):
        # RagasResult.metrics is {metric_name: float}
        ragas_out = dict(ragas.metrics)  # type: ignore[union-attr]

    # Retrieval: None when skipped (RetrievalSkipError was caught) or not run.
    retrieval_out: Optional[dict] = None
    if retrieval is not None:
        retrieval_out = {
            "recall": retrieval.recall,
            "mrr": retrieval.mrr,
            "coverage": retrieval.coverage,
            "n_questions": retrieval.n_questions,
            "k": retrieval.k,
        }

    # Zero answerable records → accuracy rates default to 0.0, which would trip
    # any floor and read as a (false) NO-GO. That is a *measurement* failure, not
    # a product failure: surface it as an error so the gate returns ERROR (exit 2)
    # with a clear reason, not "contains 0.0 < floor" (E3).
    if measurement_error is None and score is not None and score.answerable_total == 0:
        measurement_error = "no answerable records in run (cannot measure accuracy)"

    return {
        "contains_rate": score.contains_rate if score is not None else 0.0,
        "strict_rate": score.strict_rate if score is not None else 0.0,
        "refusal_rate": score.refusal_rate if score is not None else 0.0,
        # LatencyResult already stores values in seconds (duration_ms / 1000).
        "latency_p95_s": latency.p95 if latency is not None else 0.0,
        "latency_max_s": latency.max if latency is not None else 0.0,
        "total_count": total_count,
        "excluded_count": excluded_count,
        "measurement_error": measurement_error,
        "ragas": ragas_out,
        "retrieval": retrieval_out,
    }
