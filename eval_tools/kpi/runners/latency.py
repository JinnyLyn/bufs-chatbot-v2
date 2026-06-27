"""Latency KPI runner — p50/p90/p95/max + per-node timing.

Offline: reads ``duration_ms`` and ``timing`` from prediction-dump records.

``done.timing`` is a **DICT** keyed by node bucket — NOT an array. The keys
are set by ``project/api/agent_stream.py:72``:
    ``{"summarize_history": 0.0, "rewrite_query": 0.0, "agent": 0.0,
       "aggregate_answers": 0.0, "other": 0.0}``
Values are stored as integers (ms) at ``agent_stream.py:137``:
    ``timing_ms = {k: int(v * 1000) for k, v in timing.items()}``

Records with no ``duration_ms`` are silently skipped for top-level stats;
records with no ``timing`` dict are skipped for per-node stats.  This is
intentional: the pinned h100-fast snapshot has ``timing: null`` (captured
before the timing field was added) but still has valid ``duration_ms``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile on a pre-sorted list.

    Returns ``0.0`` for an empty list (no latency data available).
    Nearest-rank definition: ``ceil(p / 100 * n)``-th value (1-indexed).
    """
    if not sorted_vals:
        return 0.0
    idx = max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LatencyResult:
    """Latency statistics in **seconds** derived from dump records.

    All percentiles (``p50``, ``p90``, ``p95``, ``max``) use the top-level
    ``duration_ms`` field.  ``per_node`` breaks down the same percentile
    set per timing-dict bucket.

    ``count`` is the number of records that contributed a valid
    ``duration_ms`` value (> 0) — zero means no latency data was available.
    """

    p50: float
    p90: float
    p95: float
    max: float
    count: int
    # {node_bucket: {stat: value_in_seconds}}
    per_node: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, float]:
        """Top-level latency stats as a plain dict (for reporting)."""
        return {"p50": self.p50, "p90": self.p90, "p95": self.p95, "max": self.max}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(records: Iterable[dict]) -> LatencyResult:
    """Compute latency percentiles from dump records' ``duration_ms`` field.

    Per-node breakdown uses the ``timing`` dict (int ms values per node
    bucket). Accepts any WS-0a dump shape — normalization is NOT required
    because latency fields (``duration_ms``, ``timing``) pass through all
    known shapes unchanged.

    Parameters
    ----------
    records:
        Iterable of raw prediction-dump dicts.

    Returns
    -------
    LatencyResult
        ``p50``, ``p90``, ``p95``, ``max`` in seconds; ``count`` of records
        with valid latency; ``per_node`` timing breakdown.
    """
    durations_s: list[float] = []
    # {node_name: [ms_values_across_records]}
    node_samples: dict[str, list[float]] = {}

    for r in records:
        ms = r.get("duration_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            durations_s.append(ms / 1000.0)

        timing = r.get("timing")
        if isinstance(timing, dict):
            for node, node_ms in timing.items():
                if isinstance(node_ms, (int, float)):
                    node_samples.setdefault(node, []).append(node_ms / 1000.0)

    durations_s.sort()

    per_node: dict[str, dict[str, float]] = {}
    for node, vals in sorted(node_samples.items()):
        vals_s = sorted(vals)
        per_node[node] = {
            "p50": _percentile(vals_s, 50),
            "p90": _percentile(vals_s, 90),
            "p95": _percentile(vals_s, 95),
            "max": max(vals_s),
        }

    return LatencyResult(
        p50=_percentile(durations_s, 50),
        p90=_percentile(durations_s, 90),
        p95=_percentile(durations_s, 95),
        max=max(durations_s) if durations_s else 0.0,
        count=len(durations_s),
        per_node=per_node,
    )
