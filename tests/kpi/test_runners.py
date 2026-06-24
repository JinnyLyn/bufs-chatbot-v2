"""Tests for eval_tools.kpi.runners — task #3 (WS-C).

Unit tests (offline, default pytest lane — ``pytest -m "not integration"``):
  - rulebased: score via pinned h100-fast fixture + answer-field alias shapes.
  - latency: p50/p90/p95/max + per-node from synthetic timing-dict records.
  - retrieval: asserts RetrievalSkipError on ``--from-predictions`` (no live deps).
  - ragas: asserts N/A sentinel returned without a judge endpoint (no live deps).
  - backend_client: DoneEvent.from_payload() shape parsing (no network).

Integration tests (live-endpoint, deselected by default):
  - retrieval live path smoke (needs Qdrant + bge-m3).
  - ragas live judge smoke (needs Ollama or Gemini).
  - backend_client SSE smoke (needs running chatbot).

Run offline only (default):
    pytest tests/kpi/test_runners.py -v

Run live tests explicitly:
    pytest -m integration tests/kpi/test_runners.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_tools.kpi.runners import latency, rulebased
from eval_tools.kpi.runners.backend_client import DoneEvent
from eval_tools.kpi.runners.ragas import METRIC_NAMES, RagasResult, RagasSentinel
from eval_tools.kpi.runners.retrieval import RetrievalSkipError

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "combined88_new_result.json"


def _fixture_records() -> list[dict]:
    """Load the pinned h100-fast snapshot (89 records, timing: null, all with duration_ms)."""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["results"]


# Synthetic records with timing dicts (the h100-fast fixture has timing: null because it
# was captured before the per-node timing field was added to the backend).
_TIMING_RECORDS: list[dict] = [
    {
        "id": "T1",
        "answerable": True,
        "question": "졸업식 날짜가 언제인가요?",
        "ground_truth": "졸업식은 2월 18일입니다.",
        "answer": "졸업식은 2월 18일에 열립니다.",
        "duration_ms": 1000,
        "timing": {
            "summarize_history": 50,
            "rewrite_query": 200,
            "agent": 600,
            "aggregate_answers": 100,
            "other": 50,
        },
    },
    {
        "id": "T2",
        "answerable": True,
        "question": "수강신청 기간은 언제인가요?",
        "ground_truth": "수강신청은 3월 5일부터입니다.",
        "answer": "수강신청 기간은 3월 1일부터입니다.",
        "duration_ms": 2000,
        "timing": {
            "summarize_history": 100,
            "rewrite_query": 300,
            "agent": 1300,
            "aggregate_answers": 200,
            "other": 100,
        },
    },
    {
        "id": "T3",
        "answerable": False,
        "question": "교수님 연락처를 알 수 있나요?",
        "ground_truth": "제공되지 않습니다.",
        "answer": "찾을 수 없습니다.",
        "duration_ms": 3000,
        "timing": None,  # intentionally absent — pre-timing backend capture
    },
]


# ---------------------------------------------------------------------------
# rulebased runner
# ---------------------------------------------------------------------------

class TestRulebased:
    def test_against_pinned_fixture(self):
        """rulebased.run() reproduces the locked parity triple on the h100-fast snapshot."""
        result = rulebased.run(_fixture_records())
        assert result.contains_count == 69
        assert result.strict_count == 54
        assert result.refusal_count == 8
        assert result.answerable_total == 81
        assert result.unanswerable_total == 8

    def test_rates_on_pinned_fixture(self):
        result = rulebased.run(_fixture_records())
        assert result.contains_rate == pytest.approx(69 / 81)
        assert result.strict_rate == pytest.approx(54 / 81)
        assert result.refusal_rate == pytest.approx(1.0)

    def test_model_answer_alias(self):
        """'model_answer' field (golden-corpus shape WS-0a) normalizes to answer."""
        records = [
            {
                "id": "X1",
                "answerable": True,
                "ground_truth": "2월 18일",
                "model_answer": "졸업식은 2월 18일에 열립니다.",
            }
        ]
        result = rulebased.run(records)
        assert result.contains_count == 1
        assert result.strict_count == 1
        assert result.answerable_total == 1

    def test_prediction_alias(self):
        """'prediction' field (legacy dump shape WS-0a) normalizes to answer."""
        records = [
            {
                "id": "X2",
                "answerable": True,
                "ground_truth": "3월 5일",
                "prediction": "3월 5일부터 신청 가능합니다.",
            }
        ]
        result = rulebased.run(records)
        assert result.contains_count == 1

    def test_unanswerable_correct_refusal(self):
        records = [
            {
                "id": "U1",
                "answerable": False,
                "ground_truth": "",
                "answer": "해당 정보는 찾을 수 없습니다.",
            }
        ]
        result = rulebased.run(records)
        assert result.unanswerable_total == 1
        assert result.refusal_count == 1

    def test_unanswerable_hallucinated(self):
        records = [
            {
                "id": "U2",
                "answerable": False,
                "ground_truth": "",
                "answer": "교수님 이메일은 prof@example.com입니다.",
            }
        ]
        result = rulebased.run(records)
        assert result.refusal_count == 0

    def test_empty_records(self):
        result = rulebased.run([])
        assert result.answerable_total == 0
        assert result.unanswerable_total == 0
        assert result.contains_rate == 0.0
        assert result.strict_rate == 0.0
        assert result.refusal_rate == 0.0

    def test_d3_answerable_not_penalized_for_refusal_words(self):
        """D3 correction: answerable item with a refusal word is still credited on facts."""
        records = [
            {
                "id": "D3",
                "answerable": True,
                "ground_truth": "수강신청은 3월 5일부터",
                # Answer contains "찾을 수 없" (refusal marker) BUT also the key fact.
                "answer": "찾을 수 없지만, 3월 5일부터 수강신청이 가능합니다.",
            }
        ]
        result = rulebased.run(records)
        # D3 correction: fact "3월5일" is present → credit the answer.
        # The buggy _eval_combined88 lineage would DROP this (subtracts is_refusal).
        assert result.contains_count == 1, (
            "D3 correction failed: answerable item with refusal word must still be "
            "credited when the ground-truth fact is present in the answer"
        )


# ---------------------------------------------------------------------------
# latency runner
# ---------------------------------------------------------------------------

class TestLatency:
    def test_basic_percentiles(self):
        """p50/p90/p95/max from 3 records with durations 1s, 2s, 3s."""
        result = latency.run(_TIMING_RECORDS)
        assert result.count == 3
        # sorted [1.0, 2.0, 3.0]; nearest-rank: p50=idx1=2.0, p90=idx2=3.0, p95=idx2=3.0
        assert result.p50 == pytest.approx(2.0)
        assert result.p90 == pytest.approx(3.0)
        assert result.p95 == pytest.approx(3.0)
        assert result.max == pytest.approx(3.0)

    def test_per_node_agent_bucket(self):
        """Per-node 'agent' bucket aggregates T1(0.6s) + T2(1.3s); T3 skipped (timing: null)."""
        result = latency.run(_TIMING_RECORDS)
        assert "agent" in result.per_node
        agent = result.per_node["agent"]
        # sorted [0.6, 1.3]; p50=idx0=0.6, max=1.3
        assert agent["p50"] == pytest.approx(0.6)
        assert agent["max"] == pytest.approx(1.3)

    def test_per_node_all_agent_stream_buckets(self):
        """All 5 node buckets from agent_stream.py appear in per_node."""
        result = latency.run(_TIMING_RECORDS)
        expected = {
            "summarize_history", "rewrite_query", "agent",
            "aggregate_answers", "other",
        }
        assert expected <= set(result.per_node.keys())

    def test_skips_none_and_zero_duration(self):
        """Records with duration_ms None or 0 are excluded from stats."""
        records = [
            {"id": "N1", "duration_ms": None},
            {"id": "N2", "duration_ms": 0},   # zero → skipped (guard is > 0)
            {"id": "N3", "duration_ms": 1500},
        ]
        result = latency.run(records)
        assert result.count == 1
        assert result.p50 == pytest.approx(1.5)

    def test_skips_null_timing_for_per_node(self):
        """Records with timing: null do not contribute to per_node."""
        records = [
            {"id": "A", "duration_ms": 1000, "timing": {"agent": 800}},
            {"id": "B", "duration_ms": 2000, "timing": None},
        ]
        result = latency.run(records)
        assert result.count == 2
        # Only record A contributes to per_node["agent"]
        assert result.per_node["agent"]["max"] == pytest.approx(0.8)

    def test_empty_records(self):
        result = latency.run([])
        assert result.count == 0
        assert result.p50 == 0.0
        assert result.max == 0.0
        assert result.per_node == {}

    def test_single_record(self):
        records = [{"id": "S1", "duration_ms": 5000, "timing": {"agent": 4000}}]
        result = latency.run(records)
        assert result.count == 1
        assert result.p50 == pytest.approx(5.0)
        assert result.p90 == pytest.approx(5.0)
        assert result.p95 == pytest.approx(5.0)
        assert result.max == pytest.approx(5.0)
        assert result.per_node["agent"]["p50"] == pytest.approx(4.0)

    def test_summary_property(self):
        result = latency.run(_TIMING_RECORDS)
        s = result.summary
        assert set(s.keys()) == {"p50", "p90", "p95", "max"}
        assert s["max"] == pytest.approx(result.max)

    def test_fixture_durations_all_present(self):
        """All 89 h100-fast records have valid duration_ms; per_node is empty (timing: null)."""
        result = latency.run(_fixture_records())
        assert result.count == 89
        assert result.max > 20.0   # plan notes max ~28.6s on h100-fast
        assert result.per_node == {}  # timing: null in the pinned snapshot


# ---------------------------------------------------------------------------
# retrieval runner — offline assertions (no live Qdrant required)
# ---------------------------------------------------------------------------

class TestRetrievalOffline:
    def test_raises_on_from_predictions(self):
        """RetrievalSkipError is raised immediately when from_predictions=True."""
        from eval_tools.kpi.runners import retrieval

        with pytest.raises(RetrievalSkipError, match="--from-predictions"):
            retrieval.run(
                [],
                qdrant_url="http://localhost:6333",
                collection="document_child_chunks",
                dense_model="BAAI/bge-m3",
                from_predictions=True,
            )

    def test_skip_fires_before_live_deps(self):
        """RetrievalSkipError fires even with a nonsense URL — no live import needed."""
        from eval_tools.kpi.runners import retrieval

        with pytest.raises(RetrievalSkipError):
            retrieval.run(
                [{"id": "Q1", "answerable": True, "question": "test",
                  "ground_truth": "2월 18일"}],
                qdrant_url="NOT_A_REAL_URL",
                collection="nonexistent",
                dense_model="nonexistent",
                from_predictions=True,
            )

    def test_skip_error_is_runtime_error(self):
        """RetrievalSkipError is a RuntimeError subclass (gate can catch generically)."""
        assert issubclass(RetrievalSkipError, RuntimeError)


# ---------------------------------------------------------------------------
# ragas runner — offline assertions (no live judge required)
# ---------------------------------------------------------------------------

class TestRagasOffline:
    def test_na_sentinel_with_no_judge_model(self):
        """N/A sentinel returned when judge_model is None."""
        from eval_tools.kpi.runners import ragas

        result = ragas.run([], judge_url=None, judge_model=None)
        assert isinstance(result, RagasSentinel)
        assert result.is_na is True

    def test_sentinel_carries_all_metric_keys(self):
        """Sentinel has an N/A string for every RAGAS metric."""
        from eval_tools.kpi.runners import ragas

        result = ragas.run([], judge_url=None, judge_model=None)
        for m in METRIC_NAMES:
            assert m in result.metrics, f"metric {m!r} missing from sentinel"
            assert "N/A" in result.metrics[m], f"metric {m!r} not marked N/A"

    def test_sentinel_with_ollama_model_but_no_url(self):
        """N/A sentinel when model is set but Ollama URL is absent."""
        from eval_tools.kpi.runners import ragas

        result = ragas.run(
            [], judge_url=None, judge_model="exaone3.5:7.8b", judge_type="ollama"
        )
        assert isinstance(result, RagasSentinel)
        assert result.is_na

    def test_sentinel_with_gemini_model_but_no_api_key(self):
        """N/A sentinel when Gemini type is set but api_key is None."""
        from eval_tools.kpi.runners import ragas

        result = ragas.run(
            [],
            judge_url=None,
            judge_model="gemini-2.5-flash",
            judge_type="gemini",
            gemini_api_key=None,
        )
        assert isinstance(result, RagasSentinel)
        assert result.is_na

    def test_sentinel_is_not_ragas_result(self):
        from eval_tools.kpi.runners import ragas

        result = ragas.run([], judge_url=None, judge_model=None)
        assert not isinstance(result, RagasResult)


# ---------------------------------------------------------------------------
# backend_client — offline shape parsing (no network)
# ---------------------------------------------------------------------------

class TestDoneEventParsing:
    def test_from_payload_full(self):
        """DoneEvent.from_payload() parses all fields correctly."""
        payload = {
            "answer": "졸업식은 2월 18일입니다.",
            "duration_ms": 7843,
            "timing": {
                "summarize_history": 50,
                "rewrite_query": 200,
                "agent": 7000,
                "aggregate_answers": 500,
                "other": 93,
            },
            "results": [{"text": "학사일정 2월 18일", "score": 0.0}],
            "source_urls": ["https://bufs.ac.kr/schedule"],
            "sub_questions": 2,
            "tool_calls": 3,
            "model": "qwen3.5:9b",
            "intent": "",
        }
        event = DoneEvent.from_payload(payload)
        assert event.answer == "졸업식은 2월 18일입니다."
        assert event.duration_ms == 7843
        assert isinstance(event.timing, dict)
        assert event.timing["agent"] == 7000
        assert len(event.results) == 1
        assert event.sub_questions == 2
        assert event.tool_calls == 3
        assert event.model == "qwen3.5:9b"

    def test_timing_is_dict_not_array(self):
        """Confirms timing is a dict (keyed by node bucket) NOT a list — agent_stream.py contract."""
        payload = {
            "answer": "ok",
            "duration_ms": 1000,
            "timing": {"summarize_history": 50, "rewrite_query": 200,
                       "agent": 600, "aggregate_answers": 100, "other": 50},
        }
        event = DoneEvent.from_payload(payload)
        assert isinstance(event.timing, dict)
        for key in ("summarize_history", "rewrite_query", "agent", "aggregate_answers", "other"):
            assert key in event.timing

    def test_null_timing_stored_as_none(self):
        """timing: null (old backend captures without timing) → stored as None."""
        event = DoneEvent.from_payload({"answer": "test", "duration_ms": 1000, "timing": None})
        assert event.timing is None

    def test_missing_fields_use_defaults(self):
        """Minimal payload with just answer + duration_ms uses sane defaults."""
        event = DoneEvent.from_payload({"answer": "test", "duration_ms": 500})
        assert event.results == []
        assert event.source_urls == []
        assert event.sub_questions == 0
        assert event.tool_calls == 0
        assert event.model == ""

    def test_empty_payload_uses_all_defaults(self):
        event = DoneEvent.from_payload({})
        assert event.answer == ""
        assert event.duration_ms == 0
        assert event.timing is None


# ---------------------------------------------------------------------------
# build_run_metrics — gate-contract shape
# ---------------------------------------------------------------------------

class TestBuildRunMetrics:
    """Verify build_run_metrics() emits the exact gate-contract keys and values."""

    def _make_score(self, contains=0.85, strict=0.67, refusal=1.0):
        from eval_tools.kpi.scorer import ScoreResult
        return ScoreResult(
            contains_rate=contains, strict_rate=strict, refusal_rate=refusal,
            answerable_total=81, contains_count=int(contains * 81),
            strict_count=int(strict * 81), unanswerable_total=8,
            refusal_count=int(refusal * 8),
        )

    def _make_latency(self, p95=10.0, max_=15.0):
        return latency.LatencyResult(p50=8.0, p90=12.0, p95=p95, max=max_, count=89)

    def test_exact_keys_present(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            total_count=89,
        )
        expected_keys = {
            "contains_rate", "strict_rate", "refusal_rate",
            "latency_p95_s", "latency_max_s",
            "total_count", "excluded_count",
            "measurement_error", "ragas", "retrieval",
        }
        assert set(out.keys()) == expected_keys

    def test_latency_in_seconds_not_ms(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(),
            latency=self._make_latency(p95=10.5, max_=22.3),
            total_count=89,
        )
        assert out["latency_p95_s"] == pytest.approx(10.5)
        assert out["latency_max_s"] == pytest.approx(22.3)

    def test_ragas_none_when_sentinel(self):
        from eval_tools.kpi.runners import build_run_metrics
        sentinel = RagasSentinel(reason="no judge", metrics={})
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            ragas=sentinel, total_count=89,
        )
        assert out["ragas"] is None

    def test_ragas_none_when_not_run(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            ragas=None, total_count=89,
        )
        assert out["ragas"] is None

    def test_ragas_dict_when_scored(self):
        from eval_tools.kpi.runners import build_run_metrics
        result = RagasResult(
            metrics={"faithfulness": 0.9, "answer_relevancy": 0.8,
                     "context_precision": 0.7, "context_recall": 0.85,
                     "answer_correctness": 0.75},
            judge_model="exaone3.5:7.8b", judge_type="ollama", n=10,
        )
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            ragas=result, total_count=89,
        )
        assert isinstance(out["ragas"], dict)
        assert out["ragas"]["faithfulness"] == pytest.approx(0.9)

    def test_retrieval_none_when_not_run(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            retrieval=None, total_count=89,
        )
        assert out["retrieval"] is None

    def test_retrieval_dict_when_scored(self):
        from eval_tools.kpi.runners import build_run_metrics
        from eval_tools.kpi.runners.retrieval import RetrievalResult
        ret = RetrievalResult(k=10, recall=0.72, mrr=0.61, coverage=0.85, n_questions=65)
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            retrieval=ret, total_count=89,
        )
        assert isinstance(out["retrieval"], dict)
        assert out["retrieval"]["recall"] == pytest.approx(0.72)
        assert out["retrieval"]["k"] == 10

    def test_measurement_error_none_on_success(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            total_count=89,
        )
        assert out["measurement_error"] is None

    def test_measurement_error_str_on_failure(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            total_count=89, excluded_count=89,
            measurement_error="backend unreachable: Connection refused",
        )
        assert out["measurement_error"] == "backend unreachable: Connection refused"

    def test_excluded_count_default_zero(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(
            score=self._make_score(), latency=self._make_latency(),
            total_count=89,
        )
        assert out["excluded_count"] == 0

    def test_none_score_gives_zero_rates(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(latency=self._make_latency(), total_count=89)
        assert out["contains_rate"] == 0.0
        assert out["strict_rate"] == 0.0
        assert out["refusal_rate"] == 0.0

    def test_none_latency_gives_zero_latency(self):
        from eval_tools.kpi.runners import build_run_metrics
        out = build_run_metrics(score=self._make_score(), total_count=89)
        assert out["latency_p95_s"] == 0.0
        assert out["latency_max_s"] == 0.0


# ---------------------------------------------------------------------------
# Integration stubs — live-endpoint only, deselected by default
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_retrieval_live_smoke():
    """Live smoke-test: retrieval runner against a running Qdrant instance.

    Requires: Qdrant reachable at $BUFS_QDRANT_URL (default http://localhost:6333)
    with the collection indexed.

    Run: pytest -m integration tests/kpi/test_runners.py::test_retrieval_live_smoke
    """
    import os

    from eval_tools.kpi.runners import retrieval

    url = os.environ.get("BUFS_QDRANT_URL", "http://localhost:6333")
    collection = os.environ.get("BUFS_QDRANT_COLLECTION", "document_child_chunks")
    dense_model = os.environ.get("BUFS_DENSE_MODEL", "BAAI/bge-m3")

    result = retrieval.run(
        _fixture_records()[:5],
        qdrant_url=url,
        collection=collection,
        dense_model=dense_model,
        k=10,
        from_predictions=False,
    )
    assert result.k == 10
    assert result.n_questions >= 1
    assert 0.0 <= result.recall <= 1.0
    assert 0.0 <= result.mrr <= 1.0


@pytest.mark.integration
def test_ragas_live_smoke():
    """Live smoke-test: RAGAS runner against a running Ollama judge.

    Requires: Ollama reachable at $RAGAS_JUDGE_URL with $RAGAS_JUDGE_MODEL.

    Run: pytest -m integration tests/kpi/test_runners.py::test_ragas_live_smoke
    """
    import os

    from eval_tools.kpi.runners import ragas

    url = os.environ.get("RAGAS_JUDGE_URL", "http://localhost:11434")
    model = os.environ.get("RAGAS_JUDGE_MODEL", "exaone3.5:7.8b")

    records = [_fixture_records()[0]]
    result = ragas.run(
        records,
        judge_url=url,
        judge_model=model,
        judge_type="ollama",
        n=1,
    )
    assert isinstance(result, RagasResult)
    assert not result.is_na
    assert result.n == 1
    for m in METRIC_NAMES:
        assert m in result.metrics


@pytest.mark.integration
def test_backend_client_live_smoke():
    """Live smoke-test: backend_client.ask() against a running chatbot.

    Requires: chatbot reachable at $BUFS_BACKEND_URL (default http://localhost:8000).

    Run: pytest -m integration tests/kpi/test_runners.py::test_backend_client_live_smoke
    """
    import os

    from eval_tools.kpi.runners.backend_client import DoneEvent, ask

    url = os.environ.get("BUFS_BACKEND_URL", "http://localhost:8000")
    event = ask(url, "졸업식 날짜가 언제인가요?")
    assert isinstance(event, DoneEvent)
    assert event.answer
    assert event.duration_ms > 0
    # timing may be None (pre-timing backend) or a node-bucket dict
    assert event.timing is None or isinstance(event.timing, dict)
