"""Unit tests for the KPI gate + baseline (WS-D) — synthetic metrics, OFFLINE.

Every branch of the gate semantics (plan §Gate Semantics, r6) is asserted with
crafted metric dicts: floor pass/fail -> exit, accuracy regression pass/fail,
config/scorer-drift skip, RAGAS/retrieval SKIPPED + required->ERROR, the
ERROR(exit 2) vs NO-GO(exit 1) distinction, the advisory toggle (advisory
NO-GO -> exit 0 + banner), N-run median/worst/every-run aggregation, and that
refusal is excluded from the regression-delta. No network, no live LLM.
"""
from __future__ import annotations

import pytest

from eval_tools.kpi import baseline as bl
from eval_tools.kpi.gate import (
    EXIT_ERROR,
    EXIT_GO,
    EXIT_NOGO,
    ERROR,
    GO,
    NOGO,
    SKIPPED,
    evaluate_gate,
)

pytestmark = pytest.mark.unit


# --- fixtures ---------------------------------------------------------------
THRESHOLDS = {
    "target_contains": 0.90,
    "contains_floor": 0.83,
    "strict_floor": 0.68,
    "refusal_floor": 1.0,
    "latency_p95_max_s": 22,
    "latency_max_s": 30,
    "regression_delta_pp": 3.0,
    "flaky_tolerance": 0.0,
}

RUN_CONTEXT = {
    "gen_model": "qwen3.5:9b",
    "num_ctx": 8192,
    "fast_refuse": True,
    "compress_threshold": None,
    "scorer_version": "1",
    "scorer_hash": "abc123",
    "testset_hash": "deadbeef",
}


def _run(
    contains=0.88,
    strict=0.72,
    refusal=1.0,
    p95=20.0,
    mx=27.0,
    total=89,
    excluded=0,
    error=None,
    ragas=None,
    retrieval=None,
):
    return {
        "contains_rate": contains,
        "strict_rate": strict,
        "refusal_rate": refusal,
        "latency_p95_s": p95,
        "latency_max_s": mx,
        "total_count": total,
        "excluded_count": excluded,
        "measurement_error": error,
        "ragas": ragas,
        "retrieval": retrieval,
    }


def _baseline(contains=0.88, strict=0.72, refusal=1.0, ctx=None):
    return bl.make_baseline(
        "h100-fast",
        ctx or RUN_CONTEXT,
        {
            "contains_rate": contains,
            "strict_rate": strict,
            "refusal_rate": refusal,
            "latency_p95_s": 20.0,
            "latency_max_s": 27.0,
        },
        n_runs=3,
    )


# ===========================================================================
# floors -> exit code
# ===========================================================================
def test_all_floors_pass_no_baseline_is_go():
    res = evaluate_gate([_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.verdict == GO
    assert res.exit_code == EXIT_GO
    # bootstrap: regression guard skipped, not failed.
    assert res.family("accuracy").details["regression"]["status"] == SKIPPED


def test_contains_below_floor_is_nogo_exit_1():
    res = evaluate_gate([_run(contains=0.80)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.verdict == NOGO
    assert res.exit_code == EXIT_NOGO
    assert res.family("accuracy").is_nogo


def test_strict_below_floor_is_nogo_exit_1():
    res = evaluate_gate([_run(strict=0.50)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO
    assert any("strict" in r for r in res.family("accuracy").reasons)


def test_latency_max_over_floor_is_nogo():
    res = evaluate_gate([_run(mx=33.0)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO
    assert res.family("latency").is_nogo


def test_latency_p95_over_floor_is_nogo():
    res = evaluate_gate([_run(p95=25.0)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO
    assert res.family("latency").is_nogo


def test_target_gap_tracked_but_never_blocks():
    # contains between contains_floor (0.83) and target_contains (0.90) -> GO + gap reported.
    res = evaluate_gate([_run(contains=0.86)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.verdict == GO
    assert res.family("accuracy").details["target_gap"] == pytest.approx(0.04)


# ===========================================================================
# refusal: floor + flaky_tolerance only (NOT regression)
# ===========================================================================
def test_refusal_below_floor_beyond_tolerance_is_nogo():
    res = evaluate_gate([_run(refusal=0.875)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO
    assert res.family("refusal").is_nogo


def test_refusal_within_flaky_tolerance_is_reported_not_nogo():
    thresholds = {**THRESHOLDS, "flaky_tolerance": 0.13}  # one flip of 8 == 0.125
    res = evaluate_gate([_run(refusal=0.875)], thresholds, gating="blocking", run_context=RUN_CONTEXT)
    assert res.verdict == GO
    assert res.family("refusal").status == GO
    assert res.family("refusal").reasons  # the "reported, not NO-GO" note is present


def test_refusal_excluded_from_regression_delta():
    # Refusal dropped vs baseline by far more than regression_delta_pp, yet still
    # meets its floor -> GO. Refusal must never feed the accuracy regression delta.
    base = _baseline(refusal=1.0)
    res = evaluate_gate(
        [_run(refusal=1.0)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT, baseline=base
    )
    assert res.verdict == GO
    assert "refusal_rate" not in res.family("accuracy").details["regression"]["deltas"]


# ===========================================================================
# regression guard (like-for-like, accuracy only)
# ===========================================================================
def test_regression_within_delta_is_go():
    base = _baseline(contains=0.88)
    res = evaluate_gate(
        [_run(contains=0.86)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT, baseline=base
    )  # 2pp drop < 3pp delta
    assert res.verdict == GO
    assert res.family("accuracy").details["regression"]["status"] == GO


def test_regression_beyond_delta_is_nogo():
    base = _baseline(contains=0.90)
    res = evaluate_gate(
        [_run(contains=0.85)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT, baseline=base
    )  # 5pp drop > 3pp delta
    assert res.verdict == NOGO
    assert res.exit_code == EXIT_NOGO
    assert res.family("accuracy").details["regression"]["status"] == NOGO


def test_config_drift_skips_regression_never_false_nogo():
    drifted = {**RUN_CONTEXT, "fast_refuse": False}  # different operating point
    base = _baseline(contains=0.95, ctx=RUN_CONTEXT)  # baseline far above current
    res = evaluate_gate(
        [_run(contains=0.86)], THRESHOLDS, gating="blocking", run_context=drifted, baseline=base
    )
    assert res.verdict == GO  # floors pass; regression skipped, not a false NO-GO
    reg = res.family("accuracy").details["regression"]
    assert reg["status"] == SKIPPED
    assert "drift" in reg["reasons"][0]


def test_scorer_hash_drift_skips_regression():
    drifted = {**RUN_CONTEXT, "scorer_hash": "ZZZ_new_scorer"}
    base = _baseline(contains=0.95, ctx=RUN_CONTEXT)
    res = evaluate_gate(
        [_run(contains=0.86)], THRESHOLDS, gating="blocking", run_context=drifted, baseline=base
    )
    assert res.family("accuracy").details["regression"]["status"] == SKIPPED


# ===========================================================================
# RAGAS / retrieval: SKIPPED vs required -> ERROR
# ===========================================================================
def test_ragas_absent_is_skipped_gate_still_go():
    res = evaluate_gate([_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.family("ragas").status == SKIPPED
    assert res.verdict == GO


def test_require_ragas_without_judge_is_error_exit_2():
    res = evaluate_gate(
        [_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT, require_ragas=True
    )
    assert res.verdict == ERROR
    assert res.exit_code == EXIT_ERROR
    assert res.family("ragas").is_error


def test_retrieval_absent_is_skipped():
    res = evaluate_gate([_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.family("retrieval").status == SKIPPED


def test_require_retrieval_on_predictions_path_is_error_exit_2():
    res = evaluate_gate(
        [_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT, require_retrieval=True
    )
    assert res.exit_code == EXIT_ERROR
    assert res.family("retrieval").is_error


# ===========================================================================
# ERROR (exit 2) vs NO-GO (exit 1)
# ===========================================================================
def test_unreachable_backend_is_error_not_nogo():
    res = evaluate_gate(
        [_run(error="backend 503 at /api/chat/stream")],
        THRESHOLDS,
        gating="blocking",
        run_context=RUN_CONTEXT,
    )
    assert res.verdict == ERROR
    assert res.exit_code == EXIT_ERROR  # NOT exit 1
    assert res.family("measurement").is_error


def test_error_budget_overrun_is_error_exit_2():
    res = evaluate_gate(
        [_run(total=89, excluded=10)],  # 11.2% > 5% default
        THRESHOLDS,
        gating="blocking",
        run_context=RUN_CONTEXT,
    )
    assert res.exit_code == EXIT_ERROR
    assert res.family("measurement").is_error


def test_excluded_within_budget_is_not_error():
    res = evaluate_gate(
        [_run(total=89, excluded=2)],  # 2.2% < 5%
        THRESHOLDS,
        gating="blocking",
        run_context=RUN_CONTEXT,
    )
    assert res.verdict == GO
    assert res.family("measurement") is None


def test_accuracy_below_floor_maps_to_exit_1_not_2():
    # The companion to the backend-unreachable case: a product failure is exit 1.
    res = evaluate_gate([_run(contains=0.10)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO


# ===========================================================================
# advisory toggle
# ===========================================================================
def test_advisory_nogo_exits_0_with_banner():
    res = evaluate_gate([_run(contains=0.10)], THRESHOLDS, gating="advisory", run_context=RUN_CONTEXT)
    assert res.verdict == NOGO  # the verdict is still computed + reported
    assert res.advisory is True
    assert res.exit_code == EXIT_GO  # but never enforced
    assert res.banner and "ADVISORY" in res.banner


def test_blocking_same_subfloor_run_exits_1():
    res = evaluate_gate([_run(contains=0.10)], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.exit_code == EXIT_NOGO
    assert res.advisory is False


def test_advisory_does_not_suppress_error_exit_2():
    res = evaluate_gate(
        [_run(error="backend down")], THRESHOLDS, gating="advisory", run_context=RUN_CONTEXT
    )
    assert res.verdict == ERROR
    assert res.exit_code == EXIT_ERROR  # advisory never downgrades a measurement ERROR


# ===========================================================================
# N-run aggregation (plan §6/§7)
# ===========================================================================
def test_accuracy_uses_median_across_n_runs():
    # median of [0.90, 0.84, 0.86] == 0.86.
    runs = [_run(contains=0.90), _run(contains=0.84), _run(contains=0.86)]
    assert evaluate_gate(runs, THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT).aggregated[
        "contains_rate"
    ] == pytest.approx(0.86)
    # median 0.86 >= floor 0.83 -> GO ...
    assert evaluate_gate(runs, THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT).verdict == GO
    # ... but a floor above the median fails (proves median, not best run, gates).
    strict_floor = {**THRESHOLDS, "contains_floor": 0.87}
    assert evaluate_gate(runs, strict_floor, gating="blocking", run_context=RUN_CONTEXT).verdict == NOGO


def test_latency_uses_worst_run_max():
    runs = [_run(mx=25.0), _run(mx=33.0), _run(mx=26.0)]  # worst 33 > floor 30
    res = evaluate_gate(runs, THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.aggregated["latency_max_s"] == 33.0
    assert res.family("latency").is_nogo


def test_refusal_must_hold_in_every_run():
    # One run flips refusal -> the every-run (min) aggregation fails the floor.
    runs = [_run(refusal=1.0), _run(refusal=1.0), _run(refusal=0.875)]
    res = evaluate_gate(runs, THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.aggregated["refusal_rate"] == 0.875
    assert res.family("refusal").is_nogo
    assert res.exit_code == EXIT_NOGO


def test_no_runs_is_error():
    res = evaluate_gate([], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    assert res.verdict == ERROR
    assert res.exit_code == EXIT_ERROR


# ===========================================================================
# baseline.py: match-key, set-floors, update eligibility, round-trip
# ===========================================================================
def test_match_keys_compatible_and_incompatible():
    key = bl.build_match_key(RUN_CONTEXT)
    assert bl.match_keys_compatible(key, dict(key))
    assert not bl.match_keys_compatible(key, {**key, "fast_refuse": False})
    assert not bl.match_keys_compatible(key, {**key, "scorer_hash": "other"})
    # compress_threshold None on both sides is a valid match (nocompress point).
    assert bl.match_keys_compatible({"compress_threshold": None}, {"compress_threshold": None})


def test_compute_floors_is_median_minus_margin():
    floors = bl.compute_floors({"contains_rate": 0.852, "strict_rate": 0.667}, regression_delta_pp=3.0)
    assert floors["contains_floor"] == pytest.approx(0.822)  # 0.852 - 0.03
    assert floors["strict_floor"] == pytest.approx(0.637)  # 0.667 - 0.03


def test_can_update_baseline_rules():
    assert bl.can_update_baseline(n_runs=3, temperature=0.0).allowed
    assert not bl.can_update_baseline(n_runs=1, temperature=0.0).allowed  # single-dump
    assert not bl.can_update_baseline(n_runs=3, temperature=0.7).allowed  # unpinned


def test_baseline_save_load_round_trip(tmp_path):
    base = _baseline(contains=0.852, strict=0.667)
    bl.save_baseline("h100-fast", base, baselines_dir=tmp_path)
    loaded = bl.load_baseline("h100-fast", baselines_dir=tmp_path)
    assert loaded == base
    assert bl.load_baseline("missing-profile", baselines_dir=tmp_path) is None


def test_loaded_baseline_is_comparable_to_its_own_context():
    base = _baseline()
    assert bl.is_comparable(RUN_CONTEXT, base)
    assert not bl.is_comparable({**RUN_CONTEXT, "num_ctx": 4096}, base)


# ===========================================================================
# overall structure sanity
# ===========================================================================
def test_summary_renders_all_families():
    res = evaluate_gate([_run()], THRESHOLDS, gating="blocking", run_context=RUN_CONTEXT)
    text = res.summary()
    for fam in ("accuracy", "refusal", "latency", "ragas", "retrieval"):
        assert fam in text
    assert "VERDICT" in text
