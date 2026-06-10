"""Unit tests for rag_agent/graph_state.py — state dataclasses and accumulator reducers.

Tests cover:
- accumulate_or_reset: normal accumulation and __reset__ sentinel
- set_union: merges two sets of retrieval keys
- State / AgentState field defaults and type annotations
"""
import pytest


def _import_graph_state():
    from rag_agent.graph_state import (
        accumulate_or_reset,
        set_union,
        State,
        AgentState,
    )
    return accumulate_or_reset, set_union, State, AgentState


class TestAccumulateOrReset:
    def test_appends_new_items_to_existing(self):
        acc, *_ = _import_graph_state()
        existing = [{"answer": "A"}]
        new = [{"answer": "B"}]
        result = acc(existing, new)
        assert result == [{"answer": "A"}, {"answer": "B"}]

    def test_reset_sentinel_clears_existing_items(self):
        acc, *_ = _import_graph_state()
        existing = [{"answer": "A"}, {"answer": "B"}]
        new = [{"__reset__": True}]
        result = acc(existing, new)
        assert result == []

    def test_empty_new_list_does_not_modify_existing(self):
        acc, *_ = _import_graph_state()
        existing = [{"answer": "A"}]
        result = acc(existing, [])
        assert result == [{"answer": "A"}]

    def test_accumulation_from_empty_existing(self):
        acc, *_ = _import_graph_state()
        result = acc([], [{"answer": "first"}])
        assert result == [{"answer": "first"}]

    def test_reset_only_triggers_if_any_item_has_reset_key(self):
        """A list without __reset__ must NOT clear existing items."""
        acc, *_ = _import_graph_state()
        existing = [{"answer": "A"}]
        new = [{"answer": "B"}, {"answer": "C"}]
        result = acc(existing, new)
        assert len(result) == 3

    def test_reset_sentinel_mixed_with_real_items_still_clears(self):
        """If any item has __reset__, the whole existing list is dropped."""
        acc, *_ = _import_graph_state()
        existing = [{"answer": "A"}]
        new = [{"__reset__": True}, {"answer": "fresh"}]
        result = acc(existing, new)
        assert result == []


class TestSetUnion:
    def test_merges_two_sets(self):
        _, su, *_ = _import_graph_state()
        result = su({"a", "b"}, {"b", "c"})
        assert result == {"a", "b", "c"}

    def test_empty_sets(self):
        _, su, *_ = _import_graph_state()
        assert su(set(), set()) == set()

    def test_disjoint_sets(self):
        _, su, *_ = _import_graph_state()
        assert su({"x"}, {"y"}) == {"x", "y"}

    def test_identical_sets_deduplicate(self):
        _, su, *_ = _import_graph_state()
        assert su({"a", "b"}, {"a", "b"}) == {"a", "b"}


class TestStateDefaults:
    """State and AgentState are TypedDicts — instances are plain dicts.
    Tests verify field names are accepted and values round-trip via dict access.
    """

    def test_state_accepts_question_is_clear_field(self):
        _, _, State, _ = _import_graph_state()
        s = State(questionIsClear=False)
        assert s["questionIsClear"] is False

    def test_state_accepts_rewritten_questions_field(self):
        _, _, State, _ = _import_graph_state()
        s = State(rewrittenQuestions=["q1", "q2"])
        assert s["rewrittenQuestions"] == ["q1", "q2"]

    def test_state_accepts_agent_answers_field(self):
        _, _, State, _ = _import_graph_state()
        s = State(agent_answers=[{"answer": "test"}])
        assert s["agent_answers"] == [{"answer": "test"}]

    def test_agent_state_accepts_iteration_count_field(self):
        _, _, _, AgentState = _import_graph_state()
        s = AgentState(iteration_count=0)
        assert s["iteration_count"] == 0

    def test_agent_state_accepts_tool_call_count_field(self):
        _, _, _, AgentState = _import_graph_state()
        s = AgentState(tool_call_count=3)
        assert s["tool_call_count"] == 3

    def test_agent_state_accepts_retrieval_keys_field(self):
        _, _, _, AgentState = _import_graph_state()
        s = AgentState(retrieval_keys={"key1", "key2"})
        assert s["retrieval_keys"] == {"key1", "key2"}
