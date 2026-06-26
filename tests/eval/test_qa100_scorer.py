"""Offline tests for the QA100 golden dataset + rule-based scorer (qa_scorer).

No backend/LLM/network — these guard the *scoring* logic, the exact place the historical
false-refusal bug lived. ``qa_scorer`` is importable via pythonpath=["eval_tools"].
"""
import pytest

import qa_scorer

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- dataset


def test_dataset_loads_and_validates():
    data = qa_scorer.load_dataset()
    assert len(data) == 100
    ids = [r["id"] for r in data]
    assert len(set(ids)) == 100  # unique
    # every record must carry at least one must_include token to be scorable
    assert all(r["must_include"] for r in data)


def test_load_dataset_rejects_duplicate_ids(tmp_path):
    import json
    bad = tmp_path / "bad.json"
    rec = {k: "" for k in qa_scorer.REQUIRED_FIELDS}
    rec.update(id=1, must_include=["x"], must_not_include=[])
    bad.write_text(json.dumps([rec, dict(rec)]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate id"):
        qa_scorer.load_dataset(str(bad))


def test_load_dataset_rejects_missing_field(tmp_path):
    import json
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": 1, "question": "q"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        qa_scorer.load_dataset(str(bad))


# --------------------------------------------------------------------------- scorer


def _rec(must_include, must_not_include=None):
    return {"must_include": must_include, "must_not_include": must_not_include or []}


def test_all_must_include_present_is_pass():
    rec = _rec(["졸업", "학부(과) 사무실"])
    res = qa_scorer.score_record(rec, "졸업 사유라면 학부(과) 사무실에 문의하세요.")
    assert res["verdict"] == "PASS"
    assert res["strict_pass"] and res["contains"]


def test_whitespace_insensitive_matching():
    # token has a space, answer omits it -> still a hit
    res = qa_scorer.score_record(_rec(["학부(과) 사무실"]), "학부(과)사무실로 문의")
    assert res["verdict"] == "PASS"


def test_partial_include_is_contains():
    res = qa_scorer.score_record(_rec(["졸업", "학부(과) 사무실"]), "졸업 요건만 언급")
    assert res["verdict"] == "CONTAINS"
    assert res["contains"] and not res["strict_pass"]


def test_no_include_is_fail():
    res = qa_scorer.score_record(_rec(["졸업"]), "전혀 관련 없는 답변")
    assert res["verdict"] == "FAIL"
    assert not res["contains"]


def test_must_not_include_is_violation_even_if_includes_present():
    rec = _rec(["졸업", "학부(과) 사무실"], ["휴학"])
    res = qa_scorer.score_record(rec, "졸업이면 학부(과) 사무실에 문의하되 휴학도 가능")
    assert res["verdict"] == "VIOLATION"
    assert res["violations"] == ["휴학"]
    assert not res["strict_pass"] and not res["contains"]


def test_refusal_words_in_correct_answer_do_not_fail():
    # Regression guard for the false-refusal bug: '불가'/'없습니다' must NOT auto-fail.
    rec = _rec(["기간 외", "정정 기간"])
    answer = "안내된 기간 외에는 추가 신청이 불가능하므로 정정 기간을 활용하세요. 별도 방법은 없습니다."
    res = qa_scorer.score_record(rec, answer)
    assert res["verdict"] == "PASS"


# --------------------------------------------------------------------------- helpers


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


def test_summarize_buckets():
    results = [
        {"category": "A", "difficulty": "Easy", "strict_pass": True, "contains": True, "verdict": "PASS"},
        {"category": "A", "difficulty": "Hard", "strict_pass": False, "contains": True, "verdict": "CONTAINS"},
        {"category": "B", "difficulty": "Easy", "strict_pass": False, "contains": False, "verdict": "VIOLATION"},
    ]
    s = qa_scorer.summarize(results)
    assert s["n"] == 3
    assert s["strict_pass"] == 1
    assert s["strict_pass_rate"] == round(1 / 3, 4)
    assert s["violation_rate"] == round(1 / 3, 4)
    assert s["by_category"]["A"]["n"] == 2
    assert s["by_difficulty"]["Easy"]["n"] == 2
