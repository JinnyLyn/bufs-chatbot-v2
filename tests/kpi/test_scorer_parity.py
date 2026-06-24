"""0-diff parity: the new ``eval_tools.kpi.scorer`` vs the corrected reference.

Acceptance Criterion #1 (N=1 single-file path). The frozen snapshot
``tests/kpi/fixtures/combined88_new_result.json`` is the deployed **h100-fast**
run (fast_refuse=ON). Scoring it through the new ``scorer.score()`` must be
**identical (0 diff)** to ``eval_tools/_aggregate_variants.py``'s ``score()``,
and must equal the committed corrected-lineage triple
``(0.852, 0.667, 1.0)`` == ``(69/81, 54/81, 8/8)``.

Deterministic, offline, no network — runs under the default
``pytest -m "not integration"`` lane.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_tools import _aggregate_variants as agg
from eval_tools.kpi import scorer

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "combined88_new_result.json"


def _records() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["results"]


def test_scorer_zero_diff_vs_aggregate_variants() -> None:
    """New scorer reproduces ``_aggregate_variants.score()`` exactly (0 diff)."""
    ref = agg.score(str(_FIXTURE))  # corrected reference, reads the file itself
    result = scorer.score(_records())

    # Raw counts identical.
    assert result.contains_count == ref["contains"]
    assert result.strict_count == ref["strict"]
    assert result.answerable_total == ref["a_tot"]
    assert result.refusal_count == ref["r_ok"]
    assert result.unanswerable_total == ref["r_tot"]

    # Rates identical (exact float arithmetic, no rounding tolerance).
    assert result.rates == (
        ref["contains"] / ref["a_tot"],
        ref["strict"] / ref["a_tot"],
        ref["r_ok"] / ref["r_tot"],
    )


def test_scorer_matches_committed_literal_triple() -> None:
    """Locked corrected-lineage values on the pinned h100-fast snapshot."""
    result = scorer.score(_records())

    # Exact fractions (the true float repr — the plan's literal is the rounded form).
    assert (result.answerable_total, result.unanswerable_total) == (81, 8)
    assert (result.contains_count, result.strict_count, result.refusal_count) == (69, 54, 8)
    assert result.rates == (69 / 81, 54 / 81, 8 / 8)

    # Plan literal (0.852, 0.667, 1.0) is the 3-decimal display of the above.
    contains_rate, strict_rate, refusal_rate = result.rates
    assert (round(contains_rate, 3), round(strict_rate, 3), round(refusal_rate, 3)) == (
        0.852, 0.667, 1.0,
    )


def test_answerable_items_not_penalized_for_refusal_words() -> None:
    """D3 correction: answerable items judged on fact-presence, never refusal-subtracted.

    Proves the corrected lineage: an answerable answer that contains a refusal
    marker (e.g. "없습니다") is still credited when its facts are present.
    """
    result = scorer.score(_records())
    answerable = [v for v in result.items if v.answerable]
    # At least one answerable answer trips the refusal-word detector yet is
    # still counted as a contains-hit (would be dropped by _eval_combined88).
    refusal_word_but_credited = [
        v for v in answerable if v.is_refusal and v.some
    ]
    assert refusal_word_but_credited, (
        "expected >=1 answerable item with a refusal word still credited "
        "(D3 correction); none found"
    )
    # Sanity: every unanswerable item in this snapshot is a correct refusal.
    unanswerable = [v for v in result.items if not v.answerable]
    assert len(unanswerable) == 8
    assert all(v.is_refusal for v in unanswerable)
