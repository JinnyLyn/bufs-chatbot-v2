"""Per-profile baseline persistence + like-for-like match-key (WS-D).

A *baseline* is the last human-accepted KPI snapshot for one profile, committed
at ``eval_tools/baselines/<profile>.json`` (tracked, NOT gitignored — the
regression reference must be reproducible and reviewable in PRs). The gate
(:mod:`eval_tools.kpi.gate`) loads it and compares the current run **only
like-for-like**: the full match-key (config knobs + scorer identity + testset
hash) must agree, otherwise the regression guard is SKIPPED rather than firing a
false NO-GO (a scorer change makes an old baseline incomparable for *non-config*
reasons).

This module is PURE: stdlib only, no ``import config``, no network. The only I/O
is reading/writing the committed baseline JSON files, so it runs in the default
offline ``pytest -m "not integration"`` lane.

Baseline JSON shape (one object)::

    {
      "profile": "h100-fast",
      "match_key": {gen_model, num_ctx, fast_refuse, compress_threshold,
                    scorer_version, scorer_hash, testset_hash},
      "metrics":   {contains_rate, strict_rate, refusal_rate,
                    latency_p95_s, latency_max_s},
      "n_runs": 3, "temperature": 0.0, "seed": 1234,
      "stability_map": {"all_stable": true},   # Phase-1 stub (WS-D2 seam)
      "run_context": {...}                      # full stamp, for audit
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

# The match-key fields. Two runs are comparable for regression ONLY if they
# agree on EVERY one of these. Config knobs (the ~6pp A-vs-B swing) + scorer
# identity (a scorer change moves numbers for non-config reasons) + testset hash.
MATCH_KEY_FIELDS: tuple[str, ...] = (
    "gen_model",
    "num_ctx",
    "fast_refuse",
    "compress_threshold",
    "scorer_version",
    "scorer_hash",
    "testset_hash",
)

# Accuracy metrics whose floors are set by ``--set-floors`` (floor = median − margin).
_FLOOR_METRICS: tuple[tuple[str, str], ...] = (
    ("contains_rate", "contains_floor"),
    ("strict_rate", "strict_floor"),
)

# Default committed baselines dir: ``<repo>/eval_tools/baselines``.
BASELINES_DIR: Path = Path(__file__).resolve().parent.parent / "baselines"


def baseline_path(profile: str, baselines_dir: Optional[Path | str] = None) -> Path:
    """Path to the committed baseline JSON for ``profile``."""
    base = Path(baselines_dir) if baselines_dir is not None else BASELINES_DIR
    return base / f"{profile}.json"


def build_match_key(run_context: Mapping[str, Any]) -> dict[str, Any]:
    """Project a run-context stamp down to the comparable match-key fields."""
    return {f: run_context.get(f) for f in MATCH_KEY_FIELDS}


def match_keys_compatible(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True iff two match-keys agree on EVERY field (like-for-like).

    Conservative: a field present on one side and absent on the other counts as
    a mismatch (``.get`` -> ``None`` differs from a real value), so an
    underspecified baseline skips the regression guard instead of producing a
    phantom comparison. ``compress_threshold == None`` on both sides is a valid
    match (the h100-fast nocompress operating point).
    """
    return all(a.get(f) == b.get(f) for f in MATCH_KEY_FIELDS)


def is_comparable(run_context: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    """Whether ``run_context`` is like-for-like with a loaded ``baseline``."""
    return match_keys_compatible(build_match_key(run_context), baseline.get("match_key", {}))


def load_baseline(
    profile: str, baselines_dir: Optional[Path | str] = None
) -> Optional[dict[str, Any]]:
    """Load the committed baseline for ``profile`` (``None`` if absent — bootstrap)."""
    path = baseline_path(profile, baselines_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(
    profile: str, baseline: Mapping[str, Any], baselines_dir: Optional[Path | str] = None
) -> Path:
    """Write ``baseline`` to the committed path; returns the path written."""
    path = baseline_path(profile, baselines_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def make_baseline(
    profile: str,
    run_context: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    n_runs: int,
    temperature: float = 0.0,
    seed: Optional[int] = None,
    stability_map: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble a baseline record from a (median-aggregated) metric view.

    ``stability_map`` defaults to the Phase-1 ``{"all_stable": true}`` stub —
    the WS-D2 seam where the N-dump FLAKY/STABLE classification will plug in.
    """
    return {
        "profile": profile,
        "match_key": build_match_key(run_context),
        "metrics": {
            "contains_rate": metrics.get("contains_rate"),
            "strict_rate": metrics.get("strict_rate"),
            "refusal_rate": metrics.get("refusal_rate"),
            "latency_p95_s": metrics.get("latency_p95_s"),
            "latency_max_s": metrics.get("latency_max_s"),
        },
        "n_runs": n_runs,
        "temperature": temperature,
        "seed": seed,
        "stability_map": dict(stability_map) if stability_map else {"all_stable": True},
        "run_context": dict(run_context),
    }


@dataclass(frozen=True)
class UpdateEligibility:
    """Whether a run may update a baseline + the human-readable reason if not."""

    allowed: bool
    reason: str = ""


def can_update_baseline(n_runs: int, temperature: float) -> UpdateEligibility:
    """Gate the ``baseline-update`` write.

    A single-dump (N=1) run can score and report but CANNOT update a baseline
    (no stability evidence). A run whose generation was not pinned
    (``temperature != 0``) is advisory-only and likewise cannot update.
    """
    if temperature != 0:
        return UpdateEligibility(False, f"temperature={temperature} != 0: advisory-only, cannot update baseline")
    if n_runs < 2:
        return UpdateEligibility(False, "single-dump (N=1) run has no stability evidence: cannot update baseline")
    return UpdateEligibility(True)


def compute_floors(metrics: Mapping[str, Any], regression_delta_pp: float) -> dict[str, float]:
    """``--set-floors``: floor = observed-median − margin (margin = regression_delta_pp).

    Returns the ``contains_floor`` / ``strict_floor`` values to write into
    ``kpi_profiles.yaml`` (clamped to ``[0, 1]``). Setting these clears the
    FLAG markers and is the trigger to flip ``gating: advisory -> blocking``
    (the YAML edit + gating flip is the CLI/report layer's job — this function
    only computes the numbers).
    """
    margin = regression_delta_pp / 100.0
    floors: dict[str, float] = {}
    for metric_key, floor_key in _FLOOR_METRICS:
        median = metrics.get(metric_key)
        if median is None:
            continue
        floors[floor_key] = round(max(0.0, min(1.0, median - margin)), 4)
    return floors
