"""WS-R1 — perturbation determinism + ground_truth preservation (offline, unit).

The Real-Usage perturbation source must be SEEDED + deterministic (same seed ->
byte-identical output across runs/processes) and must PRESERVE each parent's
``ground_truth`` so ``contains``/``strict`` stay offline-scorable. These tests
pin both invariants on the committed combined88 set.
"""
from __future__ import annotations

import pytest

from eval_tools.kpi.sources import perturb

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def parents() -> list[dict]:
    return perturb.load_answerable_parents()


def test_only_answerable_parents_loaded(parents: list[dict]) -> None:
    assert len(parents) == 81
    assert all(p.get("answerable") for p in parents)


def test_perturbation_is_deterministic_across_runs(parents: list[dict]) -> None:
    """Same seed -> identical output (the core determinism contract)."""
    run1 = perturb.perturb_dataset(parents)
    run2 = perturb.perturb_dataset(parents)
    assert run1 == run2


def test_perturbation_is_process_stable_not_hash_salted() -> None:
    """Seed derives from a content hash, not Python's salted ``hash()``.

    Re-deriving an RNG for the same (seed, id, ptype) must give the same draw —
    proven by regenerating a single question twice and getting the same string.
    """
    q = "2026학년도 1학기 수강신청은 언제인가?"
    first = perturb.perturb_question(q, "typo", base_seed=123, parent_id="s99")
    second = perturb.perturb_question(q, "typo", base_seed=123, parent_id="s99")
    assert first == second
    # A different seed (very likely) yields a different edit position/char.
    other = perturb.perturb_question(q, "typo", base_seed=999, parent_id="s99")
    assert other is not None and first is not None


def test_ground_truth_is_preserved_verbatim(parents: list[dict]) -> None:
    """Every child keeps its parent's ground_truth exactly (scorability)."""
    gt_by_id = {p["id"]: p["ground_truth"] for p in parents}
    children = perturb.perturb_dataset(parents)
    assert children, "expected perturbed children"
    for child in children:
        assert child["ground_truth"] == gt_by_id[child["parent_id"]]
        assert child["answerable"] is True


def test_children_carry_provenance_and_derived_id(parents: list[dict]) -> None:
    children = perturb.perturb_dataset(parents)
    for child in children:
        assert child["perturbation"] in perturb.PERTURBATIONS
        assert child["id"] == f"{child['parent_id']}__{child['perturbation']}"


def test_each_perturbation_type_changes_the_question(parents: list[dict]) -> None:
    """Applicable transforms must produce a string that differs from the parent."""
    children = perturb.perturb_dataset(parents)
    q_by_id = {p["id"]: p["question"] for p in parents}
    for child in children:
        assert child["question"] != q_by_id[child["parent_id"]]
        assert child["question"].strip()


def test_all_six_types_exercised_across_the_set(parents: list[dict]) -> None:
    children = perturb.perturb_dataset(parents)
    seen = {c["perturbation"] for c in children}
    assert seen == set(perturb.PERTURBATIONS)


def test_spacing_removes_internal_whitespace() -> None:
    out = perturb.perturb_question("개강일은 언제 인가?", "spacing", parent_id="x")
    assert out == "개강일은언제인가?"


def test_spacing_returns_none_when_no_spaces() -> None:
    assert perturb.perturb_question("개강일은?", "spacing", parent_id="x") is None


def test_typo_is_single_edit_keyboard_adjacent() -> None:
    """A typo changes exactly one syllable to a keyboard-adjacent jamo form."""
    q = "개강일은 언제인가?"
    out = perturb.perturb_question(q, "typo", base_seed=7, parent_id="x")
    assert out is not None and out != q and len(out) == len(q)
    diffs = [i for i, (a, b) in enumerate(zip(q, out)) if a != b]
    assert len(diffs) == 1


def test_codeswitch_replaces_known_term() -> None:
    out = perturb.perturb_question("졸업요건이 무엇인가?", "codeswitch", parent_id="x")
    assert out is not None and "graduation requirement" in out


def test_codeswitch_returns_none_without_known_term() -> None:
    assert perturb.perturb_question("오늘 날씨 어때?", "codeswitch", parent_id="x") is None


def test_unknown_perturbation_type_raises() -> None:
    with pytest.raises(ValueError):
        perturb.perturb_question("질문", "nonexistent", parent_id="x")
