"""Unit tests for issue-#145 처방 1 — user-slot extraction.

Covers:
- format_user_slots: empty/partial rendering, deterministic order, "" contract
  (empty block → no injection → OFF-identical trajectory).
- extract_user_slots: gated call, model_dump passthrough, failure → {} + logged.
- rewrite_query: userSlots in the state update (ON) / absent extraction call (OFF).
- orchestrator / aggregate_answers: injection present only when slots exist.
- route_after_rewrite: user_slots carried in the Send payload.
"""
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import config
from rag_agent import edges, nodes
from rag_agent.schemas import UserSlots


SLOTS = {"admission_year": "2024학번", "leave_type": "일반휴학", "extra": ["등록금 일부만 납부"]}


class _StructuredLLM:
    """FakeLLM exposing with_structured_output like ChatOllama."""

    def __init__(self, result):
        self._result = result
        self.structured_calls = 0

    def with_structured_output(self, schema, method=None):
        outer = self

        class _Runner:
            def invoke(self, messages):
                outer.structured_calls += 1
                if isinstance(outer._result, Exception):
                    raise outer._result
                return outer._result

        return _Runner()

    def invoke(self, messages, **kwargs):
        return AIMessage(content="fake answer")


# ---------------------------------------------------------------------------
# format_user_slots
# ---------------------------------------------------------------------------

class TestFormatUserSlots:
    def test_empty_dict_renders_empty(self):
        assert nodes.format_user_slots({}) == ""

    def test_all_blank_fields_render_empty(self):
        blank = UserSlots().model_dump()
        assert nodes.format_user_slots(blank) == ""

    def test_partial_fields_render_labeled_lines(self):
        text = nodes.format_user_slots(SLOTS)
        assert text.startswith("[사용자 상황 조건]")
        assert "- 학번/입학년도: 2024학번" in text
        assert "- 휴학 유형: 일반휴학" in text
        assert "- 기타 조건: 등록금 일부만 납부" in text
        # unstated fields are absent, not rendered as blanks
        assert "학적 신분" not in text

    def test_order_is_deterministic(self):
        text = nodes.format_user_slots(
            {"leave_type": "병역휴학", "admission_year": "17학번"})
        assert text.index("학번/입학년도") < text.index("휴학 유형")

    def test_whitespace_only_values_skipped(self):
        assert nodes.format_user_slots({"grade": "  ", "extra": ["", "  "]}) == ""


# ---------------------------------------------------------------------------
# extract_user_slots
# ---------------------------------------------------------------------------

class TestExtractUserSlots:
    def test_returns_model_dump(self):
        llm = _StructuredLLM(UserSlots(admission_year="2024학번"))
        slots = nodes.extract_user_slots(llm, "2024학번인데 휴학 연장 되나요?")
        assert slots["admission_year"] == "2024학번"
        assert slots["extra"] == []

    def test_empty_question_skips_llm(self):
        llm = _StructuredLLM(UserSlots())
        assert nodes.extract_user_slots(llm, "  ") == {}
        assert llm.structured_calls == 0

    def test_failure_degrades_to_empty_and_logs(self, caplog):
        llm = _StructuredLLM(RuntimeError("boom"))
        with caplog.at_level(logging.ERROR, logger="rag_agent.nodes"):
            assert nodes.extract_user_slots(llm, "질문") == {}
        assert any("slot extraction failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# rewrite_query integration (rewrite OFF path — the live default)
# ---------------------------------------------------------------------------

class TestRewriteQuerySlots:
    def _state(self, q="2024학번인데 휴학 연장 되나요?"):
        return {"messages": [HumanMessage(content=q)], "conversation_summary": ""}

    def test_off_no_extraction_no_key_pollution(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        monkeypatch.setattr(config, "REWRITE_ENABLED", False)
        llm = _StructuredLLM(UserSlots(admission_year="2024학번"))
        result = nodes.rewrite_query(self._state(), llm)
        assert llm.structured_calls == 0
        assert result["userSlots"] == {}

    def test_on_populates_userSlots(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(config, "REWRITE_ENABLED", False)
        llm = _StructuredLLM(UserSlots(admission_year="2024학번"))
        result = nodes.rewrite_query(self._state(), llm)
        assert llm.structured_calls == 1
        assert result["userSlots"]["admission_year"] == "2024학번"
        assert result["questionIsClear"] is True  # base contract untouched


# ---------------------------------------------------------------------------
# orchestrator injection
# ---------------------------------------------------------------------------

class _CapturingToolsLLM:
    def __init__(self):
        self.messages = None

    def invoke(self, messages, **kwargs):
        self.messages = messages
        return AIMessage(content="draft")


class TestOrchestratorInjection:
    def test_slots_injected_on_first_turn(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        llm = _CapturingToolsLLM()
        nodes.orchestrator({"question": "휴학 연장 되나요?", "user_slots": SLOTS}, llm)
        joined = "\n".join(m.content for m in llm.messages)
        assert "[사용자 상황 조건]" in joined
        assert "2024학번" in joined

    def test_no_slots_no_injection(self, monkeypatch):
        """Scoping contract: slot-free question → prompt sequence identical to OFF."""
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        on = _CapturingToolsLLM()
        nodes.orchestrator({"question": "휴학 최대 기간은?", "user_slots": {}}, on)
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        off = _CapturingToolsLLM()
        nodes.orchestrator({"question": "휴학 최대 기간은?", "user_slots": {}}, off)
        assert [m.content for m in on.messages] == [m.content for m in off.messages]

    def test_disabled_ignores_stale_slots(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        llm = _CapturingToolsLLM()
        nodes.orchestrator({"question": "q", "user_slots": SLOTS}, llm)
        joined = "\n".join(m.content for m in llm.messages)
        assert "[사용자 상황 조건]" not in joined


# ---------------------------------------------------------------------------
# aggregate_answers injection
# ---------------------------------------------------------------------------

class _CapturingLLM:
    def __init__(self):
        self.messages = None

    def invoke(self, messages, **kwargs):
        self.messages = messages
        return AIMessage(content="final")


class TestAggregationInjection:
    def _state(self, slots):
        return {"originalQuery": "휴학 연장 되나요?", "userSlots": slots,
                "agent_answers": [{"index": 0, "question": "q", "answer": "a"}]}

    def test_slots_appended_with_coverage_instruction(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        llm = _CapturingLLM()
        nodes.aggregate_answers(self._state(SLOTS), llm)
        user = llm.messages[1].content
        assert "[사용자 상황 조건]" in user
        assert "조건이 답변에 반영되었는지" in user

    def test_no_slots_input_identical_to_off(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        on = _CapturingLLM()
        nodes.aggregate_answers(self._state({}), on)
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        off = _CapturingLLM()
        nodes.aggregate_answers(self._state({}), off)
        assert on.messages[1].content == off.messages[1].content


# ---------------------------------------------------------------------------
# route_after_rewrite carries the slots
# ---------------------------------------------------------------------------

class TestSendCarriesSlots:
    def test_send_payload_includes_user_slots(self):
        state = {"questionIsClear": True, "rewrittenQuestions": ["q1"], "userSlots": SLOTS}
        sends = edges.route_after_rewrite(state)
        assert sends[0].arg["user_slots"] == SLOTS

    def test_send_payload_defaults_to_empty(self):
        state = {"questionIsClear": True, "rewrittenQuestions": ["q1"]}
        sends = edges.route_after_rewrite(state)
        assert sends[0].arg["user_slots"] == {}
