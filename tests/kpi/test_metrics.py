"""Offline unit tests for the QA KPI module (Accuracy/Precision/Recall/F1/Faithfulness).

Pure/offline. Confirms the TP/FP/FN/TN framing and the lexical faithfulness proxy.
"""
import pytest

from eval_tools.kpi.metrics import MetricSet, compute, gave_answer, render_markdown

pytestmark = pytest.mark.unit


def ctx(*texts):
    return [{"text": t, "doc": "doc", "score": 0.9} for t in texts]


def case(**over):
    c = dict(
        id=1, must_include=["3년"], must_not_include=[], expected_answer="휴학은 3년까지",
        gold_document="doc", answerable=True,
        answer="휴학은 3년까지 가능합니다", retrieved=ctx("휴학은 3년까지 가능합니다"),
    )
    c.update(over)
    return c


def test_gave_answer_distinguishes_refusal_and_blank():
    assert gave_answer("휴학은 3년까지 가능합니다") is True
    assert gave_answer("") is False
    assert gave_answer("확인할 수 없습니다") is False


def test_true_positive_answered_and_correct():
    m = compute([case()])
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 0, 0, 0)
    assert m.accuracy == 1.0 and m.precision == 1.0 and m.recall == 1.0 and m.f1 == 1.0


def test_false_positive_answered_but_wrong():
    m = compute([case(answer="휴학은 5년까지 가능합니다")])
    assert (m.tp, m.fp, m.fn, m.tn) == (0, 1, 0, 0)
    assert m.precision == 0.0


def test_false_negative_refused_when_answerable():
    m = compute([case(answer="확인할 수 없습니다")])
    assert (m.tp, m.fp, m.fn, m.tn) == (0, 0, 1, 0)
    assert m.recall == 0.0


def test_true_negative_correctly_refused_unanswerable():
    m = compute([case(answerable=False, must_include=[], expected_answer="없습니다",
                      answer="제공된 문서에서 찾을 수 없습니다", retrieved=[])])
    assert (m.tp, m.fp, m.fn, m.tn) == (0, 0, 0, 1)
    assert m.accuracy == 1.0


def test_unanswerable_answered_is_false_positive():
    m = compute([case(answerable=False, must_include=[], expected_answer="없습니다",
                      answer="오늘은 김치찌개입니다", retrieved=[])])
    assert (m.tp, m.fp, m.fn, m.tn) == (0, 1, 0, 0)


def test_precision_recall_f1_mixed():
    cases = [
        case(id=1),                                   # TP
        case(id=2, answer="휴학은 5년까지"),            # FP
        case(id=3, answer="확인할 수 없습니다"),         # FN
        case(id=4, answerable=False, must_include=[], expected_answer="없습니다",
             answer="찾을 수 없습니다", retrieved=[]),   # TN
    ]
    m = compute(cases)
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 1, 1, 1)
    assert m.precision == 0.5 and m.recall == 0.5 and m.f1 == 0.5
    assert m.accuracy == 0.5


def test_faithfulness_grounded_vs_hallucinated():
    grounded = compute([case(answer="휴학은 3년까지 가능합니다",
                             retrieved=ctx("휴학은 3년까지 가능합니다"))])
    assert grounded.faithfulness == 1.0 and grounded.n_faithfulness == 1
    # answer asserts the gold fact but the context does NOT contain it -> unsupported
    ungrounded = compute([case(answer="휴학은 3년까지 가능합니다",
                               retrieved=ctx("전혀 관련 없는 장학금 안내"))])
    assert ungrounded.faithfulness == 0.0


def test_faithfulness_none_when_no_contexts():
    m = compute([case(retrieved=[])])
    assert m.faithfulness is None and m.n_faithfulness == 0


def test_retrieval_recall_and_precision():
    miss = [{"text": "무관한 내용", "doc": "다른 문서", "score": 0.4}]  # wrong doc + no gold fact
    cases = [
        case(id=1, retrieved=ctx("휴학은 3년까지 가능합니다")),  # evidence retrieved, answered
        case(id=2, retrieved=miss, answer="확인할 수 없습니다"),  # miss, refused
    ]
    m = compute(cases)
    assert m.retrieval_recall == pytest.approx(0.5, abs=1e-4)   # 1 of 2 items had evidence
    assert m.retrieval_precision == pytest.approx(1.0, abs=1e-4)  # of answered items, evidence present


def test_render_markdown_contains_all_kpis():
    md = render_markdown(compute([case()]))
    for kpi in ("Accuracy", "Precision", "Recall", "F1", "Faithfulness"):
        assert kpi in md


def test_metricset_as_dict_roundtrips_keys():
    d = compute([case()]).as_dict()
    assert {"accuracy", "precision", "recall", "f1", "faithfulness"} <= set(d)


def test_correct_answer_with_negation_word_is_not_a_false_refusal():
    """A correct answer containing a negation word (…휴학이 불가능합니다) must count as a
    real answer (TP), not a punt — the scorer.py D3 false-refusal trap."""
    c = case(must_include=["휴학 불가"], expected_answer="…휴학이 불가능합니다",
             answer="등록금 완납 전까지는 휴학 불가능합니다", retrieved=ctx("휴학 불가능"))
    m = compute([c])
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 0, 0, 0)


def test_accuracy_matches_error_analysis_definition():
    """metrics.Accuracy = (TP+TN)/N must equal error_analysis's is_correct rate."""
    from eval_tools.kpi.error_analysis import analyze
    cases = [
        case(id=1),                                                  # TP
        case(id=2, answer="휴학은 5년까지"),                          # FP
        case(id=3, answer="확인할 수 없습니다"),                       # FN
        case(id=4, answerable=False, must_include=[], expected_answer="없습니다",
             answer="찾을 수 없습니다", retrieved=[]),                 # TN
        # correct answer that contains a negation word (false-refusal guard)
        case(id=5, must_include=["휴학 불가"], expected_answer="휴학 불가",
             answer="완납 전까지 휴학 불가합니다", retrieved=ctx("휴학 불가")),
    ]
    assert compute(cases).accuracy == pytest.approx(analyze(cases).accuracy, abs=1e-9)
