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
