"""Offline tests for the QA100 golden dataset + rule-based guard (qa_scorer).

No backend/LLM/network. The rule layer enforces `must_not_include` only (a forbidden-phrase
guard); `must_include` correctness is delegated to RAGAS/LLM-judge against `expected_answer`.
`qa_scorer` is importable via pythonpath=["eval_tools"].
"""
import json
import os

import pytest

import qa_scorer

pytestmark = pytest.mark.unit

_DATASETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "eval_tools", "datasets")
# Every shipped golden set must be schema-valid and gold-self-consistent, not just the default.
_ALL_DATASETS = ["qa_dataset.json", "qa_dataset_factual100.json", "qa_dataset_factual100_variants.json"]


# --------------------------------------------------------------------------- dataset


def test_dataset_loads_and_validates():
    data = qa_scorer.load_dataset()
    assert len(data) == 100
    assert len(set(r["id"] for r in data)) == 100  # unique ids


def test_gold_answers_pass_their_own_guard():
    """Integrity: a correct expected_answer must never trip its own must_not_include guard
    (the negation false-positive Codex flagged). Guarantees the rule layer has no built-in
    false negatives from the gold itself."""
    data = qa_scorer.load_dataset()
    bad = [r["id"] for r in data if not qa_scorer.score_record(r, r["expected_answer"])["clean"]]
    assert bad == [], f"gold answers violating their own must_not_include: {bad}"


@pytest.mark.parametrize("fname", _ALL_DATASETS)
def test_all_shipped_datasets_valid_and_self_consistent(fname):
    """Every golden set (default + factual100 + variants) must load, have unique ids, and no
    gold expected_answer may trip its own must_not_include guard (substring false-positive)."""
    path = os.path.join(_DATASETS_DIR, fname)
    if not os.path.exists(path):
        pytest.skip(f"{fname} not present")
    data = qa_scorer.load_dataset(path)
    assert data, f"{fname} is empty"
    assert len(set(r["id"] for r in data)) == len(data), f"{fname} has duplicate ids"
    bad = [r["id"] for r in data if not qa_scorer.score_record(r, r["expected_answer"])["clean"]]
    assert bad == [], f"{fname}: gold answers violating their own must_not_include: {bad}"


def test_variant_dataset_maps_to_factual100_bases():
    """Each variant's base_id must exist in factual100 and inherit its gold answer (single-sourced)."""
    vpath = os.path.join(_DATASETS_DIR, "qa_dataset_factual100_variants.json")
    if not os.path.exists(vpath):
        pytest.skip("variants not present")
    base = {r["id"]: r for r in qa_scorer.load_dataset(os.path.join(_DATASETS_DIR, "qa_dataset_factual100.json"))}
    variants = json.load(open(vpath, encoding="utf-8"))
    for v in variants:
        assert v["base_id"] in base, f"variant {v['id']} → unknown base_id {v['base_id']}"
        assert v["expected_answer"] == base[v["base_id"]]["expected_answer"], f"variant {v['id']} gold drifted from base"


def test_load_dataset_rejects_duplicate_ids(tmp_path):
    bad = tmp_path / "bad.json"
    rec = {k: "" for k in qa_scorer.REQUIRED_FIELDS}
    rec.update(id=1, must_include=["x"], must_not_include=[])
    bad.write_text(json.dumps([rec, dict(rec)]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate id"):
        qa_scorer.load_dataset(str(bad))


def test_load_dataset_rejects_missing_field(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": 1, "question": "q"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        qa_scorer.load_dataset(str(bad))


# --------------------------------------------------------------------------- guard scorer


def _rec(must_not_include=None):
    return {"must_not_include": must_not_include or []}


def test_no_forbidden_token_is_clean():
    res = qa_scorer.score_record(_rec(["휴학", "자퇴"]), "졸업 사유라면 학부(과) 사무실에 문의하세요.")
    assert res["verdict"] == "CLEAN"
    assert res["clean"] and res["violations"] == []


def test_forbidden_token_is_violation():
    res = qa_scorer.score_record(_rec(["휴학"]), "휴학을 권장합니다.")
    assert res["verdict"] == "VIOLATION"
    assert not res["clean"] and res["violations"] == ["휴학"]


def test_empty_must_not_include_is_clean():
    assert qa_scorer.score_record(_rec([]), "어떤 답변이든")["clean"]


def test_violation_is_whitespace_insensitive():
    res = qa_scorer.score_record(_rec(["학부(과) 사무실"]), "학부(과)사무실 로 가세요")
    assert res["verdict"] == "VIOLATION"


def test_refusal_words_do_not_cause_violation():
    # Regression guard for the false-refusal bug: '불가'/'없습니다' are not forbidden tokens.
    res = qa_scorer.score_record(_rec(["휴학"]), "추가 신청은 불가능하며 별도 방법은 없습니다.")
    assert res["verdict"] == "CLEAN"


def test_must_include_is_not_rule_scored():
    # must_include is intentionally ignored by the rule scorer (delegated to LLM judge).
    rec = {"must_include": ["답변에-절대-없는-토큰"], "must_not_include": []}
    assert qa_scorer.score_record(rec, "전혀 다른 답변")["clean"]


# --------------------------------------------------------------------------- helpers


def test_contains_is_contiguous():
    assert qa_scorer.contains("직접 신청", "직접신청 안내")           # whitespace-insensitive substring
    assert not qa_scorer.contains("직접 신청", "직접 시스템에서 신청")  # not contiguous


def test_tokens_present_is_order_independent():
    assert qa_scorer.tokens_present("직접 신청", "직접 시스템에서 신청")  # both words present, gapped
    assert not qa_scorer.tokens_present("직접 신청", "직접 처리만")
    assert qa_scorer.tokens_present("4월·10월", "4월 또는 10월")          # splits on middot


def test_intent_match_soft():
    assert qa_scorer.intent_match("수강신청 정원 외 신청", "수강신청 정원 외 신청")
    assert qa_scorer.intent_match("수강신청", "수강신청 정원 외 신청")
    assert not qa_scorer.intent_match("수강신청", "장학금 안내")
    assert not qa_scorer.intent_match("수강신청", None)


def test_doc_recall_matches_title_to_filename():
    dr = qa_scorer.doc_recall("2026학년도 1학기 학사안내", ["2026학년도1학기학사안내.md", "수강신청 FAQ.md"])
    assert dr["hit"]
    assert dr["matched_sources"] == ["2026학년도1학기학사안내.md"]
    assert not qa_scorer.doc_recall("학사경고", ["수강신청 FAQ.md"])["hit"]


# --------------------------------------------------------------------------- summarize


def test_summarize_buckets():
    results = [
        {"category": "A", "difficulty": "Easy", "clean": True, "verdict": "CLEAN"},
        {"category": "A", "difficulty": "Hard", "clean": True, "verdict": "CLEAN"},
        {"category": "B", "difficulty": "Easy", "clean": False, "verdict": "VIOLATION"},
    ]
    s = qa_scorer.summarize(results)
    assert s["n"] == 3
    assert s["clean"] == 2
    assert s["clean_rate"] == round(2 / 3, 4)
    assert s["violation_rate"] == round(1 / 3, 4)
    assert s["by_category"]["A"]["n"] == 2
    assert s["by_category"]["A"]["clean_rate"] == 1.0
    assert s["by_difficulty"]["Easy"]["n"] == 2


def test_summarize_gates_intent_and_recall():
    # KPIs only appear when at least one row was actually evaluated (Codex comment #1).
    base = {"category": "A", "difficulty": "Easy", "clean": True, "verdict": "CLEAN"}
    s = qa_scorer.summarize([base, base])
    assert "intent_accuracy" not in s and "retrieval_recall" not in s
    s2 = qa_scorer.summarize([
        {**base, "intent_evaluated": True, "intent_correct": True},
        {**base, "intent_evaluated": True, "intent_correct": False},
    ])
    assert s2["intent_accuracy"] == 0.5 and s2["intent_evaluated"] == 2
