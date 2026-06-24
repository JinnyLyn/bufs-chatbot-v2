"""KPI gate — per-family GO / NO-GO / ERROR verdict + process exit code (WS-D).

PURE + OFFLINE: consumes already-computed metric dicts (synthetic in unit
tests, runner output in production) plus a profile threshold dict and an
optional baseline. No ``import config``, no network — runs in the default
``pytest -m "not integration"`` lane. The gate is a standalone decision
function the CLI calls and maps to ``sys.exit(result.exit_code)``; it is NOT a
pytest target, so it can never be silently deselected.

Gate Semantics (plan §Gate Semantics, r6):

1. **Absolute floors** — each family's metric must meet its profile floor.
2. **Regression guard (like-for-like)** — compare current vs baseline ONLY if
   the full match-key agrees (config knobs + scorer identity + testset hash);
   drift -> ``REGRESSION: SKIPPED`` (never a false NO-GO). Applied to the
   accuracy KPIs only; NOT to the refusal family (sub-granular at N=8) and not
   to latency.
3. **Phase-1.5 DEFERRED** — variance weighting is behind the :class:`StabilityMap`
   seam; the Phase-1 stub treats every question STABLE and runs PLAIN
   regression-delta on the full set. Adding the N-dump map later is additive.
4. **Refusal** — gated by ``refusal_floor`` + ``flaky_tolerance`` ONLY.
5. **Fail-open/closed** — measurement failure -> ERROR (exit 2); product
   failure -> NO-GO (exit 1); all pass -> GO (exit 0). RAGAS / retrieval absent
   -> family SKIPPED unless ``require_*`` (then ERROR). Excluded-question
   error-budget (default 5%) overrun -> ERROR.
6. **N-run aggregation** — accuracy = median across runs; latency = worst-run;
   refusal must hold in EVERY run (min across runs).
7. **Advisory-only** — when ``gating == "advisory"`` the gate computes + reports
   the full verdict but NEVER exits 1 (advisory NO-GO -> exit 0 + banner);
   ERROR still exits 2.

Per-run metric dict (the unit of input; pass one for N=1, many for N-run)::

    {
      "contains_rate": 0.85, "strict_rate": 0.66, "refusal_rate": 1.0,
      "latency_p95_s": 21.0, "latency_max_s": 28.0,
      "total_count": 89, "excluded_count": 0,
      "measurement_error": None,   # truthy string -> ERROR for the whole run
      "ragas": None,               # dict if measured, else None -> SKIPPED
      "retrieval": None,           # dict if measured, else None -> SKIPPED
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Mapping, Optional, Sequence

from . import baseline as _baseline

# --- family status + overall verdict constants -----------------------------
GO = "GO"
NOGO = "NO-GO"
SKIPPED = "SKIPPED"
ERROR = "ERROR"

# --- exit codes (plan §5) ---------------------------------------------------
EXIT_GO = 0
EXIT_NOGO = 1
EXIT_ERROR = 2

# Accuracy KPIs subject to the regression guard. Refusal + latency are
# floor-only by design (refusal is sub-granular at N=8; latency regression is
# not specified). See plan §2/§7.
_REGRESSION_KPIS: tuple[str, ...] = ("contains_rate", "strict_rate")

DEFAULT_ERROR_BUDGET = 0.05


# ===========================================================================
# Phase-1.5 seam: stability map
# ===========================================================================
class StabilityMap:
    """Per-question FLAKY/STABLE classification (plan §3, Phase-1.5).

    The regression delta is computed on the STABLE subset, and a *flaky*
    refusal flip is reported rather than auto-NO-GO. In Phase 1 this is a stub
    (:class:`AllStableMap`) so the gate runs PLAIN regression-delta on the full
    aggregate; the WS-D2 N-dump-seeded map swaps in additively.
    """

    def is_flaky(self, qid: Any) -> bool:  # pragma: no cover - overridden in WS-D2
        raise NotImplementedError

    def is_stable(self, qid: Any) -> bool:
        return not self.is_flaky(qid)


class AllStableMap(StabilityMap):
    """Phase-1 stub: no question is flaky (full-set regression, hard floors)."""

    def is_flaky(self, qid: Any) -> bool:
        return False


# ===========================================================================
# result types
# ===========================================================================
@dataclass(frozen=True)
class FamilyVerdict:
    """Per-KPI-family verdict."""

    name: str
    status: str  # GO | NO-GO | SKIPPED | ERROR
    reasons: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status == ERROR

    @property
    def is_nogo(self) -> bool:
        return self.status == NOGO


@dataclass(frozen=True)
class GateResult:
    """Overall gate outcome + the process exit code the CLI should return."""

    verdict: str  # GO | NO-GO | ERROR
    exit_code: int
    advisory: bool
    gating: str
    families: tuple[FamilyVerdict, ...]
    banner: Optional[str] = None
    aggregated: Mapping[str, Any] = field(default_factory=dict)
    n_runs: int = 0

    def family(self, name: str) -> Optional[FamilyVerdict]:
        for fam in self.families:
            if fam.name == name:
                return fam
        return None

    def summary(self) -> str:
        lines = [f"VERDICT: {self.verdict} (exit {self.exit_code})"]
        if self.banner:
            lines.append(self.banner)
        for fam in self.families:
            reason = f" — {'; '.join(fam.reasons)}" if fam.reasons else ""
            lines.append(f"  [{fam.status:<7}] {fam.name}{reason}")
        return "\n".join(lines)


# ===========================================================================
# aggregation (plan §6/§7)
# ===========================================================================
def _present(runs: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [r[key] for r in runs if r.get(key) is not None]


def aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Collapse N per-run metric dicts to one view.

    accuracy -> median across runs; latency -> worst (max) run; refusal ->
    every-run (min, a safety property, not the median).
    """
    agg: dict[str, Any] = {}
    for key in ("contains_rate", "strict_rate"):
        vals = _present(runs, key)
        agg[key] = median(vals) if vals else None
    refusal_runs = _present(runs, "refusal_rate")
    agg["refusal_rate"] = min(refusal_runs) if refusal_runs else None
    agg["refusal_runs"] = refusal_runs
    for key in ("latency_p95_s", "latency_max_s"):
        vals = _present(runs, key)
        agg[key] = max(vals) if vals else None
    return agg


# ===========================================================================
# per-family evaluators
# ===========================================================================
def _regression(
    agg: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    run_context: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Like-for-like accuracy regression vs baseline. Returns a status block."""
    if not baseline:
        return {"status": SKIPPED, "reasons": ("no baseline for profile (bootstrap)",), "deltas": {}}
    if not _baseline.is_comparable(run_context, baseline):
        return {
            "status": SKIPPED,
            "reasons": ("config/scorer drift — regression not comparable",),
            "deltas": {},
        }
    delta_pp = float(thresholds.get("regression_delta_pp", 0.0) or 0.0)
    thresh = delta_pp / 100.0
    base_metrics = baseline.get("metrics", {})
    reasons: list[str] = []
    deltas: dict[str, float] = {}
    status = GO
    for key in _REGRESSION_KPIS:
        cur = agg.get(key)
        base = base_metrics.get(key)
        if cur is None or base is None:
            continue
        drop = base - cur  # positive == regression (got worse)
        deltas[key] = round(drop, 6)
        if drop > thresh:
            status = NOGO
            reasons.append(
                f"{key} regressed {drop * 100:.1f}pp > {delta_pp:.1f}pp "
                f"(baseline {base:.3f} -> current {cur:.3f})"
            )
    return {"status": status, "reasons": tuple(reasons), "deltas": deltas}


def _accuracy_family(
    agg: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    run_context: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
) -> FamilyVerdict:
    """Rule-based accuracy: contains/strict floors + accuracy regression guard."""
    reasons: list[str] = []
    status = GO
    contains = agg.get("contains_rate")
    strict = agg.get("strict_rate")
    details: dict[str, Any] = {"contains_rate": contains, "strict_rate": strict}

    cf = thresholds.get("contains_floor")
    if cf is not None and contains is not None and contains < cf:
        status = NOGO
        reasons.append(f"contains {contains:.3f} < contains_floor {cf:.3f}")
    sf = thresholds.get("strict_floor")
    if sf is not None and strict is not None and strict < sf:
        status = NOGO
        reasons.append(f"strict {strict:.3f} < strict_floor {sf:.3f}")

    # Aspirational target gap — tracked, NEVER blocks.
    tc = thresholds.get("target_contains")
    if tc is not None and contains is not None:
        details["target_gap"] = round(tc - contains, 6)

    reg = _regression(agg, thresholds, run_context, baseline)
    details["regression"] = reg
    if reg["status"] == NOGO:
        status = NOGO
        reasons.extend(reg["reasons"])

    return FamilyVerdict("accuracy", status, tuple(reasons), details)


def _refusal_family(agg: Mapping[str, Any], thresholds: Mapping[str, Any]) -> FamilyVerdict:
    """Refusal floor (every-run) routed through ``flaky_tolerance`` only.

    Never subject to ``regression_delta_pp`` (§2/§4). With only 8 unanswerable
    questions a single flip is a 12.5pp cliff, so a refusal that dips below the
    floor but stays within ``flaky_tolerance`` is REPORTED, not auto-NO-GO.
    """
    floor = thresholds.get("refusal_floor")
    tol = float(thresholds.get("flaky_tolerance", 0.0) or 0.0)
    worst = agg.get("refusal_rate")  # min across runs (every-run safety)
    details = {
        "refusal_rate_worst": worst,
        "per_run": agg.get("refusal_runs", []),
        "flaky_tolerance": tol,
    }
    if floor is None or worst is None:
        return FamilyVerdict("refusal", SKIPPED, ("no refusal_floor / no refusal metric",), details)
    if worst >= floor:
        return FamilyVerdict("refusal", GO, (), details)
    if worst >= floor - tol:
        return FamilyVerdict(
            "refusal",
            GO,
            (
                f"refusal {worst:.3f} < floor {floor:.3f} but within flaky_tolerance "
                f"{tol:.3f} — reported, not NO-GO",
            ),
            details,
        )
    return FamilyVerdict(
        "refusal",
        NOGO,
        (f"refusal {worst:.3f} < floor {floor:.3f} (beyond flaky_tolerance {tol:.3f})",),
        details,
    )


def _latency_family(agg: Mapping[str, Any], thresholds: Mapping[str, Any]) -> FamilyVerdict:
    """Latency floors on worst-run p95 + max (a tail guarantee)."""
    reasons: list[str] = []
    status = GO
    p95 = agg.get("latency_p95_s")
    mx = agg.get("latency_max_s")
    details = {"latency_p95_s": p95, "latency_max_s": mx}
    p95f = thresholds.get("latency_p95_max_s")
    mxf = thresholds.get("latency_max_s")
    if p95f is None and mxf is None:
        return FamilyVerdict("latency", SKIPPED, ("no latency floors configured",), details)
    if p95f is not None and p95 is not None and p95 > p95f:
        status = NOGO
        reasons.append(f"latency_p95 {p95:.1f}s > {p95f}s")
    if mxf is not None and mx is not None and mx > mxf:
        status = NOGO
        reasons.append(f"latency_max {mx:.1f}s > {mxf}s")
    return FamilyVerdict("latency", status, tuple(reasons), details)


def _optional_family(
    name: str,
    runs: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, Any],
    required: bool,
) -> FamilyVerdict:
    """RAGAS / retrieval: absent -> SKIPPED (or ERROR if required).

    When present, any ``<name>_*`` floors in the profile are enforced; with no
    such floors ratified in Phase 1 the family is measured + reported (GO).
    """
    measured = [r[name] for r in runs if r.get(name) is not None]
    if not measured:
        if required:
            return FamilyVerdict(name, ERROR, (f"{name} required (--require-{name}) but not measured",), {})
        return FamilyVerdict(name, SKIPPED, (f"{name} not measured",), {})

    floors = {k: v for k, v in thresholds.items() if k.startswith(f"{name}_")}
    reasons: list[str] = []
    status = GO
    # Aggregate measured metric dicts by worst (min) per key for floor checks.
    keys = {k for d in measured for k in d}
    agg_metrics: dict[str, float] = {}
    for k in keys:
        vals = [d[k] for d in measured if isinstance(d.get(k), (int, float))]
        if vals:
            agg_metrics[k] = min(vals)
    for floor_key, floor_val in floors.items():
        metric_key = floor_key[len(name) + 1 :].removesuffix("_floor")
        cur = agg_metrics.get(metric_key)
        if cur is not None and floor_val is not None and cur < floor_val:
            status = NOGO
            reasons.append(f"{metric_key} {cur:.3f} < {floor_key} {floor_val:.3f}")
    return FamilyVerdict(
        name, status, tuple(reasons), {"measured": True, "metrics": agg_metrics, "floors_applied": bool(floors)}
    )


def _measurement_family(
    runs: Sequence[Mapping[str, Any]], error_budget: float
) -> Optional[FamilyVerdict]:
    """ERROR family for measurement failures (backend down, error-budget overrun).

    Returns ``None`` when measurement is clean. A measurement failure is "the
    release decision is unknown", NOT "the product failed the bar" — it maps to
    ERROR (exit 2), never NO-GO (exit 1).
    """
    reasons: list[str] = []
    for i, r in enumerate(runs):
        err = r.get("measurement_error")
        if err:
            reasons.append(f"run[{i}]: {err}")
        total = r.get("total_count") or 0
        excluded = r.get("excluded_count") or 0
        if total and excluded / total > error_budget:
            reasons.append(
                f"run[{i}]: excluded {excluded}/{total} "
                f"({excluded / total:.1%}) > error_budget {error_budget:.0%}"
            )
    if reasons:
        return FamilyVerdict("measurement", ERROR, tuple(reasons), {})
    return None


# ===========================================================================
# entrypoint
# ===========================================================================
def evaluate_gate(
    runs: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, Any],
    *,
    gating: str = "blocking",
    run_context: Optional[Mapping[str, Any]] = None,
    baseline: Optional[Mapping[str, Any]] = None,
    require_ragas: bool = False,
    require_retrieval: bool = False,
    error_budget: float = DEFAULT_ERROR_BUDGET,
    stability_map: Optional[StabilityMap] = None,
) -> GateResult:
    """Evaluate the gate over ``runs`` and return a :class:`GateResult`.

    ``gating`` is the profile-level mode: ``"advisory"`` (h100-fast until floors
    measured, and 4090-local always) computes the full verdict but never exits
    1; ``"blocking"`` enforces. ERROR (exit 2) is never suppressed by advisory.
    """
    runs = list(runs)
    run_context = dict(run_context or {})
    stability_map = stability_map or AllStableMap()  # Phase-1 stub; seam for WS-D2

    if not runs:
        fam = FamilyVerdict("measurement", ERROR, ("no runs / dumps provided",), {})
        return GateResult(ERROR, EXIT_ERROR, False, gating, (fam,), banner="ERROR (nothing to measure)")

    families: list[FamilyVerdict] = []

    measurement = _measurement_family(runs, error_budget)
    if measurement is not None:
        families.append(measurement)

    agg = aggregate_runs(runs)
    families.append(_accuracy_family(agg, thresholds, run_context, baseline))
    families.append(_refusal_family(agg, thresholds))
    families.append(_latency_family(agg, thresholds))
    families.append(_optional_family("ragas", runs, thresholds, require_ragas))
    families.append(_optional_family("retrieval", runs, thresholds, require_retrieval))

    has_error = any(f.is_error for f in families)
    has_nogo = any(f.is_nogo for f in families)

    if has_error:
        return GateResult(
            ERROR, EXIT_ERROR, False, gating, tuple(families),
            banner="ERROR (could not measure — release decision unknown)",
            aggregated=agg, n_runs=len(runs),
        )
    if has_nogo:
        if gating == "advisory":
            return GateResult(
                NOGO, EXIT_GO, True, gating, tuple(families),
                banner="ADVISORY (floors unmeasured): NO-GO computed but NOT enforced (exit 0)",
                aggregated=agg, n_runs=len(runs),
            )
        return GateResult(
            NOGO, EXIT_NOGO, False, gating, tuple(families),
            banner=None, aggregated=agg, n_runs=len(runs),
        )
    return GateResult(
        GO, EXIT_GO, False, gating, tuple(families),
        banner=None, aggregated=agg, n_runs=len(runs),
    )
