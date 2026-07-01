"""Offline unit tests for the 7-category 오답 분석 classifier.

Pure/offline (no backend/LLM/network). Each test pins one failure signal so the
heuristic triage stays stable and auditable.
"""
import pytest

from eval_tools.kpi.error_analysis import (
    CORRECT,
    ERROR_CATEGORIES,
    AnalysisReport,
    analyze,
    classify_error,
    extract_retrieved,
    is_correct,
    join_predictions,
    render_markdown,
)

pytestmark = pytest.mark.unit


def ctx(*pairs):
    return [{"text": t, "doc": d, "score": s} for (t, d, s) in pairs]


def base(**over):
    c = dict(
        id=1, question="휴학 몇 년까지 돼", category="휴학",
        must_include=["3년"], must_not_include=[], expected_answer="휴학은 3년까지 가능",
        gold_document="재학생 및 복학생 등록 안내", answerable=True,
        answer="휴학은 3년까지 가능합니다",
        retrieved=ctx(("휴학은 3년까지 가능합니다", "재학생 및 복학생 등록 안내", 0.9)),
    )
    c.update(over)
    return c


# ───────────────────────── correctness ─────────────────────────
def test_correct_answer_is_not_an_error():
    assert classify_error(base()).code == CORRECT
    assert classify_error(base()).is_error is False


def test_explicit_correct_field_wins():
    assert is_correct(base(answer="완전히 틀린 답", correct=True)) is True
    assert is_correct(base(correct=False)) is False


def test_must_not_include_violation_is_wrong():
    c = base(must_not_include=["자퇴"], answer="휴학은 3년까지 가능하며 자퇴도 됩니다")
    assert is_correct(c) is False


# ───────────────────────── the 7 buckets ─────────────────────────
def test_no_document_when_nothing_retrieved():
    assert classify_error(base(answer="글쎄요 잘...", retrieved=[])).code == "NO_DOCUMENT"


def test_prompt_fail_evidence_present_but_refused():
    c = base(answer="확인할 수 없습니다")  # refusal despite evidence in ctx
    assert classify_error(c).code == "PROMPT"


def test_prompt_fail_evidence_present_but_blank():
    assert classify_error(base(answer="")).code == "PROMPT"


def test_hallucination_evidence_present_but_wrong():
    c = base(answer="휴학은 최대 5년까지 가능합니다")  # substantive but contradicts ctx
    assert classify_error(c).code == "HALLUCINATION"


def test_chunk_problem_gold_doc_retrieved_without_fact():
    c = base(
        answer="아무 때나 가능합니다",
        retrieved=ctx(("이 문서의 다른 단락: 복학 절차 소개", "재학생 및 복학생 등록 안내", 0.7)),
    )
    assert classify_error(c).code == "CHUNK"


def test_retrieval_fail_relevant_chunks_but_missing_gold():
    # high query↔context overlap, but neither the gold doc nor the gold fact present
    c = base(
        question="휴학 몇 년까지 돼",
        answer="바로 됩니다",
        retrieved=ctx(("휴학 몇 년까지 되는지 관련 다른 안내 문단", "다른 문서", 0.6)),
    )
    assert classify_error(c).code == "RETRIEVAL_FAIL"


def test_embedding_problem_low_lexical_overlap():
    c = base(
        question="계절학기 뭐 열려",
        must_include=["학사공지"], expected_answer="학사공지 개설 목록",
        gold_document="2026학년도 1학기 학사안내",
        answer="아직 안 열렸어요",
        retrieved=ctx(("장학금 신청 서류 제출 근로 기관 안내", "장학공지", 0.4)),
    )
    assert classify_error(c).code == "EMBEDDING"


def test_ambiguous_terse_query_scattered_retrieval():
    c = base(
        question="복전",
        must_include=["제2전공"], expected_answer="제2전공 신청",
        gold_document="2026학년도 1학기 학사안내",
        answer="무엇을 도와드릴까요",
        retrieved=ctx(("등록금 안내", "등록", 0.5), ("졸업 요건", "졸업", 0.5), ("성적 정정", "성적", 0.5)),
    )
    assert classify_error(c).code == "AMBIGUOUS"


def test_unanswerable_answered_is_hallucination():
    c = base(question="학식 메뉴", answerable=False, must_include=[],
             expected_answer="확인할 수 없습니다", gold_document="",
             answer="오늘은 김치찌개입니다", retrieved=[])
    assert classify_error(c).code == "HALLUCINATION"


def test_unanswerable_refused_is_correct():
    c = base(question="셔틀 시간", answerable=False, must_include=[],
             expected_answer="찾을 수 없습니다", gold_document="",
             answer="제공된 문서에서 찾을 수 없습니다", retrieved=[])
    assert classify_error(c).code == CORRECT


# ───────────────────────── extraction + aggregation ─────────────────────────
def test_extract_retrieved_handles_aliases_and_strings():
    rec = {"results": [
        {"page_content": "본문", "metadata": {"source": "doc.pdf"}, "similarity": 0.8},
        "plain string chunk",
    ]}
    got = extract_retrieved(rec)
    assert got[0]["text"] == "본문" and got[0]["doc"] == "doc.pdf" and got[0]["score"] == 0.8
    assert got[1]["text"] == "plain string chunk"


def test_analyze_counts_and_distribution():
    cases = [base(), base(id=2, answer="", ), base(id=3, answer="틀린 5년", )]
    rep = analyze(cases)
    assert isinstance(rep, AnalysisReport)
    assert rep.total == 3 and rep.correct == 1 and rep.wrong == 2
    assert rep.distribution.get("PROMPT") == 1
    assert rep.distribution.get("HALLUCINATION") == 1
    assert rep.accuracy == pytest.approx(1 / 3, abs=1e-4)


def test_distribution_keys_are_known_categories():
    rep = analyze([base(id=i, retrieved=[], answer="몰라요") for i in range(5)])
    assert set(rep.distribution) <= set(ERROR_CATEGORIES)


def test_join_predictions_left_joins_on_id_and_marks_missing_wrong():
    dataset = [base(id=10), base(id=11)]
    preds = [{"id": 10, "answer": "휴학은 3년까지 가능합니다",
              "results": [{"text": "휴학은 3년까지 가능합니다", "source": "재학생 및 복학생 등록 안내"}]}]
    cases = join_predictions(dataset, preds)
    assert cases[0]["answer"].startswith("휴학은 3년")
    assert cases[1]["answer"] == ""  # id 11 had no prediction -> empty
    rep = analyze(cases)
    assert rep.wrong == 1  # the dropped question


def test_render_markdown_lists_every_category():
    md = render_markdown(analyze([base(), base(id=2, retrieved=[], answer="몰라요")]))
    for label in ERROR_CATEGORIES.values():
        assert label in md
