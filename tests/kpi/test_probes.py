"""WS-R4/R5 — offline-testable probe logic + gap KPI + langfuse mining (unit).

Covers the *pure* halves of the Real-Usage probes (tokenizer stability,
clarity confusion matrix, fast-refuse over/under rates, rewrite term-drift),
the ``real_usage`` family aggregator + headline gap KPI, and the Langfuse miner's
PII/dedupe/bucket/skip logic. The live halves (kiwi over a real index, the LLM
``is_clear`` decision, live Qdrant recall) are integration-marked and deselected
by default; their *arithmetic* is exercised here via injected stubs.
"""
from __future__ import annotations

import pytest

from eval_tools.kpi import real_usage, scorer
from eval_tools.kpi.probes import clarity, refuse, rewrite, tokenizer
from eval_tools.kpi.sources import langfuse_mine

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# tokenizer probe
# --------------------------------------------------------------------------- #


def test_token_stability_identical_query_is_perfect() -> None:
    s = tokenizer.token_stability("졸업 요건 학점", "졸업 요건 학점")
    assert s.jaccard == 1.0
    assert s.retained == 1.0
    assert s.dropped == () and s.added == ()


def test_token_stability_detects_churn() -> None:
    s = tokenizer.token_stability("졸업 요건 학점", "졸업 요건 credit")
    assert 0.0 < s.jaccard < 1.0
    assert "학점" in s.dropped
    assert "credit" in s.added


def test_recall_at_k_hit_and_miss() -> None:
    assert tokenizer.recall_at_k(["d3", "d1", "d9"], {"d1"}, k=10) == 1.0
    assert tokenizer.recall_at_k(["d3", "d9"], {"d1"}, k=10) == 0.0
    # gold outside the top-k window is a miss.
    assert tokenizer.recall_at_k(["d3", "d9", "d1"], {"d1"}, k=2) == 0.0
    assert tokenizer.recall_at_k(["d3"], set(), k=10) == 0.0


def test_recall_drop_pp_sign() -> None:
    assert tokenizer.recall_drop_pp(1.0, 0.7) == pytest.approx(30.0)
    assert tokenizer.recall_drop_pp(0.7, 0.7) == 0.0


def test_recall_drop_aggregate_with_stub_retriever() -> None:
    """Perturbed queries lose the gold doc -> positive recall_drop_pp."""
    index = {"clean-q": ["g1", "x"], "typo-q": ["x", "y"]}
    pairs = [{"clean": "clean-q", "variant": "typo-q", "parent_id": "p1"}]
    drop = tokenizer.recall_drop(
        pairs,
        retrieve_fn=lambda q: index.get(q, []),
        gold_for=lambda pid: {"g1"},
        k=10,
    )
    assert drop.clean_recall == 1.0
    assert drop.perturbed_recall == 0.0
    assert drop.drop_pp == pytest.approx(100.0)
    assert drop.n_pairs == 1


# --------------------------------------------------------------------------- #
# clarity probe
# --------------------------------------------------------------------------- #


def test_clarity_confusion_matrix_balanced() -> None:
    items = [
        {"label": "clear", "is_clear": True},        # tp
        {"label": "clear", "is_clear": False},       # fn (false-clarify)
        {"label": "ambiguous", "is_clear": True},    # fp (false-answer)
        {"label": "ambiguous", "is_clear": False},   # tn
    ]
    m = clarity.confusion_matrix(items)
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 1, 1, 1)
    assert m.precision == pytest.approx(0.5)
    assert m.recall == pytest.approx(0.5)
    assert m.false_clarify_rate == pytest.approx(0.5)
    assert m.false_answer_rate == pytest.approx(0.5)


def test_clarity_perfect_gate() -> None:
    items = [
        {"label": "clear", "is_clear": True},
        {"label": "ambiguous", "is_clear": False},
    ]
    m = clarity.confusion_matrix(items)
    assert m.precision == 1.0 and m.recall == 1.0
    assert m.false_clarify_rate == 0.0 and m.false_answer_rate == 0.0


def test_clarity_accepts_bool_labels() -> None:
    items = [{"label": True, "is_clear": True}, {"label": False, "is_clear": True}]
    m = clarity.confusion_matrix(items)
    assert (m.tp, m.fp) == (1, 1)


def test_clarity_evaluate_with_stub_decider() -> None:
    labelled = [
        {"question": "개강일은 언제인가?", "label": "clear"},
        {"question": "그거 어떻게 함?", "label": "ambiguous"},
    ]
    # Stub gate: a question is "clear" iff it is long-ish (a toy heuristic).
    m = clarity.evaluate(labelled, decide_is_clear=lambda q: len(q) >= 10)
    assert m.total == 2


# --------------------------------------------------------------------------- #
# fast-refuse probe
# --------------------------------------------------------------------------- #


def test_refuse_over_and_under() -> None:
    items = [
        {"answerable": True, "answer": "2026년 3월 2일입니다."},      # answered ok
        {"answerable": True, "answer": "확인할 수 없습니다."},        # over-refuse
        {"answerable": False, "answer": "제공된 자료에는 없습니다."},  # refused ok
        {"answerable": False, "answer": "네 가능합니다."},           # under-refuse
    ]
    m = refuse.refuse_rates(items)
    assert (m.answerable_total, m.over_refuse) == (2, 1)
    assert (m.unanswerable_total, m.under_refuse) == (2, 1)
    assert m.over_refuse_rate == pytest.approx(0.5)
    assert m.under_refuse_rate == pytest.approx(0.5)


def test_refuse_honours_explicit_refused_flag() -> None:
    items = [{"answerable": True, "refused": True, "answer": "anything"}]
    m = refuse.refuse_rates(items)
    assert m.over_refuse == 1


def test_refuse_uses_canonical_is_refusal() -> None:
    """Refusal detection delegates to scorer.is_refusal (single source of truth)."""
    assert scorer.is_refusal("확인할 수 없습니다.")
    m = refuse.refuse_rates([{"answerable": False, "answer": "확인할 수 없습니다."}])
    assert m.under_refuse == 0  # correctly refused -> not an under-refuse


def test_refuse_evaluate_with_stub_answerer() -> None:
    items = [{"answerable": False, "question": "오늘 날씨?"}]
    m = refuse.evaluate(items, answer_for=lambda it: "제공된 자료에는 없습니다.")
    assert m.under_refuse == 0 and m.unanswerable_total == 1


# --------------------------------------------------------------------------- #
# rewrite probe
# --------------------------------------------------------------------------- #


def test_rewrite_term_drift_positive_when_rewrite_hurts() -> None:
    index = {"raw": ["g1"], "REW:raw": ["other"]}
    queries = [{"question": "raw", "parent_id": "p1"}]
    d = rewrite.term_drift(
        queries,
        retrieve_fn=lambda q: index.get(q, []),
        gold_for=lambda pid: {"g1"},
        rewrite_fn=lambda q: "REW:" + q,
        k=10,
    )
    assert d.recall_off == 1.0
    assert d.recall_on == 0.0
    assert d.drift_pp == pytest.approx(100.0)


def test_rewrite_term_drift_negative_when_rewrite_helps() -> None:
    index = {"raw": [], "REW:raw": ["g1"]}
    queries = [{"question": "raw", "parent_id": "p1"}]
    d = rewrite.term_drift(
        queries,
        retrieve_fn=lambda q: index.get(q, []),
        gold_for=lambda pid: {"g1"},
        rewrite_fn=lambda q: "REW:" + q,
    )
    assert d.drift_pp == pytest.approx(-100.0)


# --------------------------------------------------------------------------- #
# real_usage family + headline gap KPI
# --------------------------------------------------------------------------- #


def _score(pairs: list[tuple[str, str]]) -> scorer.ScoreResult:
    """Score (ground_truth, answer) answerable pairs."""
    records = [
        {"id": i, "answerable": True, "ground_truth": gt, "answer": ans}
        for i, (gt, ans) in enumerate(pairs)
    ]
    return scorer.score(records)


def test_benchmark_real_gap_pp_basic() -> None:
    assert real_usage.benchmark_real_gap_pp(0.85, 0.70) == pytest.approx(15.0)


def test_real_usage_from_scores_gap_and_metrics() -> None:
    clean = _score([("2026년 3월 2일", "3월 2일입니다"), ("5학점", "5학점")])
    real = _score([("2026년 3월 2일", "3월 2일입니다"), ("5학점", "모르겠습니다")])
    fam = real_usage.from_scores(clean, real, real_source="perturb")
    assert clean.contains_rate == 1.0
    assert real.contains_rate == 0.5
    assert fam.benchmark_real_gap_pp == pytest.approx(50.0)
    assert fam.exceeds_gap_floor() is True            # 50pp > 10pp advisory floor
    metrics = fam.as_metrics()
    assert metrics["benchmark_real_gap_pp"] == pytest.approx(50.0)
    # Unmeasured probes are None (honest skip), not a pass.
    assert metrics["clarity_precision"] is None
    assert metrics["recall_drop_pp"] is None


def test_real_usage_attaches_probe_outputs() -> None:
    clean = _score([("5학점", "5학점")])
    real = _score([("5학점", "5학점")])
    clarity_m = clarity.confusion_matrix([{"label": "clear", "is_clear": True}])
    refuse_m = refuse.refuse_rates([{"answerable": True, "answer": "5학점"}])
    fam = real_usage.from_scores(clean, real, clarity=clarity_m, refuse=refuse_m)
    assert fam.benchmark_real_gap_pp == pytest.approx(0.0)
    assert fam.exceeds_gap_floor() is False
    d = fam.as_dict()
    assert d["clarity"] is not None
    assert d["refuse"] is not None
    assert d["recall_drop"] is None  # not provided -> None


# --------------------------------------------------------------------------- #
# langfuse_mine — PII / dedupe / bucket / skip
# --------------------------------------------------------------------------- #


def test_strip_pii_email_phone_id_name() -> None:
    clean, changed = langfuse_mine.strip_pii(
        "제 이메일은 a.b@test.ac.kr, 전화 010-1234-5678, 학번 20231234, 홍길동 학생입니다"
    )
    assert changed
    assert "[EMAIL]" in clean and "[PHONE]" in clean
    assert "[ID]" in clean and "[NAME]" in clean
    assert "a.b@test.ac.kr" not in clean and "20231234" not in clean


def test_strip_pii_noop_when_clean() -> None:
    clean, changed = langfuse_mine.strip_pii("졸업요건이 무엇인가요?")
    assert not changed and clean == "졸업요건이 무엇인가요?"


def test_bucket_intent_taxonomy() -> None:
    assert langfuse_mine.bucket_intent("수강신청 정정 기간 언제?") == "REGISTRATION"
    assert langfuse_mine.bucket_intent("졸업요건 학점 알려줘") == "GRADUATION_REQ"
    assert langfuse_mine.bucket_intent("휴학 신청 어떻게?") == "LEAVE_OF_ABSENCE"
    assert langfuse_mine.bucket_intent("장학금 종류?") == "SCHOLARSHIP"
    assert langfuse_mine.bucket_intent("오늘 점심 뭐 먹지") == "GENERAL"


def test_process_traces_dedupes_and_buckets() -> None:
    traces = [
        {"id": "t1", "input": "학번 20231234 인데 졸업요건 알려주세요", "output": "답"},
        {"id": "t2", "input": {"question": "수강신청 언제야?"}, "output": {"answer": "3월"}},
        {"id": "t3", "input": "수강신청 언제야?", "output": "중복"},   # dup of t2
        {"id": "t4", "input": "", "output": "x"},                    # no question
        {"id": "t5", "input": "홍길동 학생입니다 장학금 문의", "output": ""},
    ]
    result = langfuse_mine.process_traces(traces)
    cov = result.coverage
    assert cov.total_raw == 5
    assert cov.skipped_no_question == 1
    assert cov.total_unique == 3                # t1, t2, t5 (t3 deduped)
    assert cov.pii_stripped == 2                # t1 (id), t5 (name)
    assert cov.without_ground_truth == 3        # production traces carry no GT
    assert cov.by_intent.get("GRADUATION_REQ") == 1
    assert cov.by_intent.get("REGISTRATION") == 1
    assert cov.by_intent.get("SCHOLARSHIP") == 1
    # PII never survives into the surfaced questions.
    assert all("20231234" not in q.question for q in result.questions)


def test_mine_skips_when_fetcher_raises() -> None:
    def boom():
        raise ConnectionError("Request URL is missing an 'http://' or 'https://' protocol.")

    res = langfuse_mine.mine(boom)
    assert isinstance(res, langfuse_mine.MineSkipped)
    assert res.status == "skipped"
    assert res.reason == "mcp_unreachable"
    assert "ConnectionError" in res.detail


def test_mine_skips_on_empty_traces() -> None:
    res = langfuse_mine.mine(lambda: [])
    assert isinstance(res, langfuse_mine.MineSkipped)
    assert res.reason == "no_traces"


def test_mine_skips_when_no_usable_questions() -> None:
    res = langfuse_mine.mine(lambda: [{"id": "t1", "input": "", "output": "x"}])
    assert isinstance(res, langfuse_mine.MineSkipped)
    assert res.reason == "no_usable_questions"


def test_mine_ok_path() -> None:
    res = langfuse_mine.mine(lambda: [{"id": "t1", "input": "졸업요건?", "output": "답"}])
    assert isinstance(res, langfuse_mine.MineResult)
    assert res.status == "ok"
    assert res.coverage.total_unique == 1
    assert res.questions[0].intent == "GRADUATION_REQ"


# --------------------------------------------------------------------------- #
# live (integration) — deselected by default
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_kiwi_tokenizer_live_stability() -> None:
    """The real kiwi tokenizer churns less than whitespace on a spacing variant."""
    pytest.importorskip("kiwipiepy")
    tok = tokenizer.kiwi_tokenizer()
    s = tokenizer.token_stability("졸업 요건은 무엇인가", "졸업요건은무엇인가", tokenizer=tok)
    assert 0.0 <= s.jaccard <= 1.0
    assert s.clean_tokens  # kiwi produced morphemes
