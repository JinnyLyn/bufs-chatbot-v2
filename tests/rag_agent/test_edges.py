"""Unit tests for rag_agent/edges.py — routing decision logic.

Tests cover:
- route_after_rewrite: unclear question → request_clarification, clear → Send list
- route_after_orchestrator_call: MAX_ITERATIONS / MAX_TOOL_CALLS boundaries,
  tool-call presence → tools, absence → collect_answer, fallback path.
"""
import importlib
import sys
from unittest.mock import MagicMock

import pytest


def _get_edges():
    """Return a freshly imported edges module (helps isolation between tests)."""
    # Reload config so tests pick up any monkeypatched env.
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    if "rag_agent.edges" in sys.modules:
        importlib.reload(sys.modules["rag_agent.edges"])
    from rag_agent import edges
    return edges


# ---------------------------------------------------------------------------
# route_after_rewrite
# ---------------------------------------------------------------------------

class TestRouteAfterRewrite:
    def test_unclear_question_routes_to_request_clarification(self):
        edges = _get_edges()
        state = {"questionIsClear": False, "rewrittenQuestions": []}
        result = edges.route_after_rewrite(state)
        assert result == "request_clarification"

    def test_missing_question_is_clear_defaults_to_clarification(self):
        """Absent key is falsy → clarification path."""
        edges = _get_edges()
        state = {"rewrittenQuestions": []}
        result = edges.route_after_rewrite(state)
        assert result == "request_clarification"

    def test_clear_question_returns_send_list(self):
        """Clear question with ≥1 rewritten query → list of Send objects."""
        edges = _get_edges()
        state = {
            "questionIsClear": True,
            "rewrittenQuestions": ["query A", "query B"],
        }
        result = edges.route_after_rewrite(state)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_send_objects_carry_correct_question_index(self):
        """Each Send carries the correct question_index."""
        edges = _get_edges()
        state = {
            "questionIsClear": True,
            "rewrittenQuestions": ["q0", "q1", "q2"],
        }
        result = edges.route_after_rewrite(state)
        # langgraph Send exposes .arg (dict), not .args
        for idx, send in enumerate(result):
            assert send.arg["question_index"] == idx

    def test_send_objects_carry_correct_question_text(self):
        edges = _get_edges()
        state = {
            "questionIsClear": True,
            "rewrittenQuestions": ["first question", "second question"],
        }
        result = edges.route_after_rewrite(state)
        assert result[0].arg["question"] == "first question"
        assert result[1].arg["question"] == "second question"

    def test_send_objects_carry_empty_messages(self):
        """Each agent sub-graph starts with an empty messages list."""
        edges = _get_edges()
        state = {
            "questionIsClear": True,
            "rewrittenQuestions": ["only question"],
        }
        result = edges.route_after_rewrite(state)
        assert result[0].arg["messages"] == []


# ---------------------------------------------------------------------------
# route_after_orchestrator_call
# ---------------------------------------------------------------------------

class TestRouteAfterOrchestratorCall:
    def _make_state(
        self,
        iteration_count: int = 0,
        tool_call_count: int = 0,
        tool_calls: list | None = None,
    ) -> dict:
        msg = MagicMock()
        msg.tool_calls = tool_calls if tool_calls is not None else []
        return {
            "iteration_count": iteration_count,
            "tool_call_count": tool_call_count,
            "messages": [msg],
        }

    def test_returns_collect_answer_when_no_tool_calls(self):
        edges = _get_edges()
        state = self._make_state(tool_calls=[])
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    def test_returns_tools_when_tool_calls_present(self):
        edges = _get_edges()
        fake_call = {"name": "search_child_chunks", "args": {}, "id": "tc1"}
        state = self._make_state(tool_calls=[fake_call])
        assert edges.route_after_orchestrator_call(state) == "tools"

    def test_returns_fallback_when_iterations_at_max(self, env_isolated, monkeypatch):
        """iteration_count >= MAX_ITERATIONS triggers fallback regardless of tool calls."""
        import importlib, config as cfg
        monkeypatch.setenv("MAX_ITERATIONS", "5")
        importlib.reload(cfg)
        edges = _get_edges()
        fake_call = {"name": "search_child_chunks", "args": {}, "id": "tc2"}
        state = self._make_state(iteration_count=5, tool_calls=[fake_call])
        assert edges.route_after_orchestrator_call(state) == "fallback_response"

    def test_returns_fallback_when_tool_calls_exceed_max(self, env_isolated, monkeypatch):
        """tool_call_count > MAX_TOOL_CALLS triggers fallback."""
        import importlib, config as cfg
        monkeypatch.setenv("MAX_TOOL_CALLS", "3")
        importlib.reload(cfg)
        edges = _get_edges()
        fake_call = {"name": "search_child_chunks", "args": {}, "id": "tc3"}
        state = self._make_state(tool_call_count=4, tool_calls=[fake_call])
        assert edges.route_after_orchestrator_call(state) == "fallback_response"

    def test_tool_call_count_at_max_does_not_trigger_fallback(self, env_isolated, monkeypatch):
        """tool_call_count == MAX_TOOL_CALLS is NOT over limit (strict >)."""
        import importlib, config as cfg
        monkeypatch.setenv("MAX_TOOL_CALLS", "8")
        importlib.reload(cfg)
        edges = _get_edges()
        fake_call = {"name": "search_child_chunks", "args": {}, "id": "tc4"}
        state = self._make_state(tool_call_count=8, tool_calls=[fake_call])
        # count == MAX is NOT > MAX; the fallback condition is strict >
        assert edges.route_after_orchestrator_call(state) == "tools"

    def test_missing_tool_calls_attribute_treated_as_no_tools(self):
        """If last message has no tool_calls attr, route to collect_answer."""
        edges = _get_edges()
        msg = MagicMock(spec=[])  # no tool_calls attribute at all
        state = {"iteration_count": 0, "tool_call_count": 0, "messages": [msg]}
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    def test_none_tool_calls_treated_as_no_tools(self):
        edges = _get_edges()
        state = self._make_state(tool_calls=None)
        state["messages"][-1].tool_calls = None
        assert edges.route_after_orchestrator_call(state) == "collect_answer"


def test_search_budget_marker_is_not_evidence():
    """#89: SEARCH_BUDGET_EXCEEDED는 검색 중단 지시이지 근거가 아니다."""
    from langchain_core.messages import ToolMessage
    from rag_agent import edges
    state = {"messages": [ToolMessage(
        content="SEARCH_BUDGET_EXCEEDED: 검색 시간 예산을 초과했습니다.", tool_call_id="t0")]}
    assert edges._has_tool_evidence(state) is False


def test_orchestrator_arms_budget_reference_only_when_lever_on(monkeypatch):
    """#89: 첫 턴에 loop_started_at 장전 — 레버 OFF면 state 불변."""
    from langchain_core.messages import AIMessage
    import config as cfg
    from rag_agent import nodes

    class _LLM:
        def invoke(self, messages, **kwargs):
            return AIMessage(content="답")

    monkeypatch.setattr(cfg, "TOOL_CALL_SOFT_TIMEOUT_S", 90.0)
    on = nodes.orchestrator({"question": "q", "messages": []}, _LLM())
    assert on.get("loop_started_at", 0) > 0

    monkeypatch.setattr(cfg, "TOOL_CALL_SOFT_TIMEOUT_S", 0.0)
    off = nodes.orchestrator({"question": "q", "messages": []}, _LLM())
    assert "loop_started_at" not in off


def test_budget_exceeded_routes_tool_request_to_fallback(monkeypatch):
    """#89: 예산 초과 후의 도구 요청은 fallback_response로 직행 — tail = 합성 1회."""
    import time as _time
    from langchain_core.messages import AIMessage
    import config as cfg
    from rag_agent import edges

    monkeypatch.setattr(cfg, "TOOL_CALL_SOFT_TIMEOUT_S", 90.0)
    msg = AIMessage(content="")
    msg.tool_calls = [{"name": "search_child_chunks", "args": {}, "id": "t1"}]
    state = {"iteration_count": 2, "tool_call_count": 2, "messages": [msg],
             "loop_started_at": _time.monotonic() - 120}
    assert edges.route_after_orchestrator_call(state) == "fallback_response"

    state["loop_started_at"] = _time.monotonic() - 1
    assert edges.route_after_orchestrator_call(state) == "tools"


def test_budget_final_answer_still_adopted_past_budget(monkeypatch):
    """#161 계약 유지: 예산이 지나도 이미 나온 최종 답변은 폐기되지 않는다."""
    import time as _time
    from langchain_core.messages import AIMessage
    import config as cfg
    from rag_agent import edges

    monkeypatch.setattr(cfg, "TOOL_CALL_SOFT_TIMEOUT_S", 90.0)
    monkeypatch.setattr(cfg, "CLEAN_SYNTHESIS_ENABLED", False)
    state = {"iteration_count": 2, "tool_call_count": 2,
             "messages": [AIMessage(content="최종 답변")],
             "loop_started_at": _time.monotonic() - 120}
    assert edges.route_after_orchestrator_call(state) == "collect_answer"
