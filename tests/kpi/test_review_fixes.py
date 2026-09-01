"""Regression tests for the PR #65 code-review fixes (behavior-changing ones).

- E3: zero-answerable run → measurement ERROR, not a false NO-GO.
- E6: benchmark↔real gap floor boundary is inclusive (>=).
- I1: a {source, records} wrapper that also carries an 'answer' key is still
  detected as a wrapper (heuristic keys on absence of 'question', not 'answer').
"""

import json

import pytest

from eval_tools.kpi.cli import _load_dump_records
from eval_tools.kpi.real_usage import from_scores
from eval_tools.kpi.runners import build_run_metrics
from eval_tools.kpi.scorer import ScoreResult, score

pytestmark = pytest.mark.unit


def _sr(contains: float, *, answerable: int = 10) -> ScoreResult:
    n = round(contains * answerable)
    return ScoreResult(
        contains_rate=contains,
        strict_rate=contains,
        refusal_rate=1.0,
        answerable_total=answerable,
        contains_count=n,
        strict_count=n,
        unanswerable_total=0,
        refusal_count=0,
    )


# ── E3 ────────────────────────────────────────────────────────────────────
def test_zero_answerable_is_measurement_error_not_nogo():
    empty = score([])  # no answerable records → rates default to 0.0
    assert empty.answerable_total == 0
    m = build_run_metrics(score=empty, total_count=0)
    assert m["measurement_error"] is not None
    assert "answerable" in m["measurement_error"].lower()


def test_normal_run_has_no_spurious_measurement_error():
    m = build_run_metrics(score=_sr(0.9), total_count=10)
    assert m["measurement_error"] is None


# ── E6 ────────────────────────────────────────────────────────────────────
def test_gap_floor_boundary_is_inclusive():
    fam = from_scores(_sr(0.90), _sr(0.80))  # ~10pp gap
    g = fam.benchmark_real_gap_pp
    assert g > 0
    assert fam.exceeds_gap_floor(g) is True          # floor == gap → flags (inclusive >=)
    assert fam.exceeds_gap_floor(g + 0.001) is False  # gap just under floor → ok


# ── I1 ────────────────────────────────────────────────────────────────────
def test_wrapper_with_answer_key_still_detected(tmp_path):
    rec = {"id": "s01", "question": "q", "ground_truth": "130", "answerable": True, "answer": "130"}
    # wrapper carries 'answer' (e.g. a summary) AND 'records' — must NOT be
    # mistaken for a bare record list.
    dump = tmp_path / "d.json"
    dump.write_text(json.dumps([{"source": "x", "answer": "summary", "records": [rec]}]), encoding="utf-8")
    assert _load_dump_records(dump) == [rec]


# ── refusal family, zero-unanswerable set (2026-09-01) ────────────────────
# Mirror of E3: an all-answerable golden set cannot measure refusal, and
# _rate(0, 0) == 0.0 read as "refused nothing" → NO-GO against refusal_floor 1.0.
def test_zero_unanswerable_reports_no_refusal_metric():
    m = build_run_metrics(score=_sr(0.9), total_count=10)  # unanswerable_total=0
    assert m["refusal_rate"] is None
    assert m["measurement_error"] is None  # accuracy is still perfectly measurable


def test_refusal_family_skips_when_metric_absent():
    from eval_tools.kpi.gate import aggregate_runs, evaluate_gate

    runs = [build_run_metrics(score=_sr(0.9), total_count=10)]
    assert aggregate_runs(runs)["refusal_rate"] is None
    verdict = evaluate_gate(runs, {"contains_floor": 0.8, "strict_floor": 0.8,
                              "refusal_floor": 1.0, "flaky_tolerance": 0.13})
    refusal = next(f for f in verdict.families if f.name == "refusal")
    assert refusal.status == "SKIPPED"
    assert verdict.exit_code != 1, "an unmeasurable refusal family must not fail the gate"


def test_refusal_still_gated_when_set_has_unanswerable():
    from eval_tools.kpi.gate import evaluate_gate

    sr = ScoreResult(contains_rate=0.9, strict_rate=0.9, refusal_rate=0.5,
                     answerable_total=10, contains_count=9, strict_count=9,
                     unanswerable_total=8, refusal_count=4)
    runs = [build_run_metrics(score=sr, total_count=18)]
    assert runs[0]["refusal_rate"] == 0.5
    verdict = evaluate_gate(runs, {"contains_floor": 0.8, "strict_floor": 0.8,
                              "refusal_floor": 1.0, "flaky_tolerance": 0.13})
    refusal = next(f for f in verdict.families if f.name == "refusal")
    assert refusal.status == "NO-GO"


# ── fact surface forms (2026-09-01 measurement) ───────────────────────────
class TestFactSurfaceForms:
    """Facts the rule scorer marked wrong on answers that were in fact correct.

    Both came out of the 2학기 골든셋 실측: the served model writes times in the
    spoken 12h form ("오후 3 시 30 분") and the gold answers carry decimals
    ("0.5학점") that were being parsed as calendar dates.
    """

    def test_decimal_is_not_a_date(self):
        from eval_tools.kpi.scorer import extract_facts

        facts = extract_facts("'진로설정(0.5학점)' 과목을 반드시 이수해야 합니다.")
        assert "0월5일" not in facts, "decimal parsed as a date makes the fact unmatchable"
        assert facts == {"0", "5"}

    def test_real_date_still_extracted(self):
        from eval_tools.kpi.scorer import extract_facts

        assert "9월15일" in extract_facts("신청은 9.15 까지입니다")

    @pytest.mark.parametrize("fact,answer", [
        ("15:30", "오후 3 시 30 분부터 신청 가능"),   # spoken 12h + spaces
        ("17:00", "오전 9 시부터 오후 5 시까지"),
        ("09:00", "오전 9 시부터 입금 가능"),
        ("09:00", "9 시부터 입금 가능"),              # unmarked bare form
        ("13:00", "13시에 시작"),                     # 24h spoken
        ("12:00", "오후 12 시"),
        ("00:30", "오전 12 시 30 분"),
    ])
    def test_equivalent_time_readings_match(self, fact, answer):
        from eval_tools.kpi.scorer import matched

        assert matched(fact, answer)

    @pytest.mark.parametrize("fact,answer", [
        ("09:00", "오후 9 시"),      # opposite meridiem must NOT satisfy the fact
        ("16:00", "오전 4 시"),
        ("15:30", "오후 4 시"),      # wrong time entirely
    ])
    def test_wrong_meridiem_does_not_match(self, fact, answer):
        from eval_tools.kpi.scorer import matched

        assert not matched(fact, answer)
