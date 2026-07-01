"""Offline integrity tests for the short/colloquial query dataset.

``eval_tools/datasets/qa_short_queries.json`` holds terse, real-student-style
queries (수강신청 언제야? / 복전 신청기간 / 군휴학 / 휴학연장 …). It reuses the same
schema as ``qa_dataset.json`` so ``qa_scorer.load_dataset`` reads it, and each
answerable record's gold facts come from a vetted ``qa_dataset`` source record
(``paraphrase_of``). No backend/LLM/network.
"""
import os

import pytest

import qa_scorer

pytestmark = pytest.mark.unit

_SHORT_PATH = os.path.join(
    os.path.dirname(qa_scorer.DATASET_PATH), "qa_short_queries.json"
)


def _load():
    return qa_scorer.load_dataset(_SHORT_PATH)


def test_loads_and_validates():
    data = _load()
    assert len(data) >= 30
    assert len(set(r["id"] for r in data)) == len(data)  # unique ids


def test_queries_are_short():
    """These are terse queries by construction — guard against a long-scenario slip-in."""
    data = _load()
    long_ones = [r["id"] for r in data if len(r["question"]) > 30]
    assert long_ones == [], f"unexpectedly long short-queries: {long_ones}"


def test_gold_answers_pass_their_own_guard():
    """A gold expected_answer must never trip its own must_not_include guard."""
    data = _load()
    bad = [r["id"] for r in data if not qa_scorer.score_record(r, r["expected_answer"])["clean"]]
    assert bad == [], f"gold answers violating their own must_not_include: {bad}"


def test_style_and_answerable_fields_present():
    data = _load()
    assert all(r.get("query_style") == "short_colloquial" for r in data)
    assert all(isinstance(r.get("answerable"), bool) for r in data)
    # a few out-of-scope refuse cases exist for the answer/refuse KPI
    assert any(r["answerable"] is False for r in data)
    assert any(r["answerable"] is True for r in data)


def test_paraphrase_sources_exist_in_qa_dataset():
    """Every answerable short query points at a real qa_dataset source id."""
    src_ids = {r["id"] for r in qa_scorer.load_dataset()}
    data = _load()
    dangling = [
        r["id"] for r in data
        if r.get("answerable") and r.get("paraphrase_of") not in src_ids
    ]
    assert dangling == [], f"paraphrase_of not found in qa_dataset: {dangling}"


def test_the_prompt_examples_are_covered():
    """The role-brief examples (복전/군휴학/휴학연장/전과/계절학기) must be present."""
    questions = " ".join(r["question"] for r in _load())
    for needle in ("복전", "군휴학", "휴학연장", "전과 신청", "계절학기", "복수전공"):
        assert needle in questions, f"missing example coverage: {needle}"
