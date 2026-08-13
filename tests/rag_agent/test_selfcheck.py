"""Unit tests for the #176 self-check lever (답변 전 자가검사).

Contract under test: JUDGE PASS ⇒ state untouched (aggregated answer byte-identical);
FAIL ⇒ one scoped rewrite; every failure mode (judge exception, empty rewrite,
verdict without findings, no evidence) degrades to keeping the draft — the node can
never lose an answer.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import config
from rag_agent import nodes
from rag_agent.schemas import SelfCheckVerdict

pytestmark = pytest.mark.unit


def _state(draft="졸업학점은 130학점입니다.", answers=None, **extra):
    return {
        "originalQuery": "졸업학점은?",
        "messages": [HumanMessage(content="졸업학점은?", id="m0"),
                     AIMessage(content=draft, id="d0")],
        "agent_answers": answers if answers is not None else [
            {"index": 0, "question": "졸업학점은?", "answer": "졸업학점은 130학점입니다."}],
        **extra,
    }


class _RewriteLLM:
    def __init__(self, content="조건부로 재작성된 답변"):
        self.content = content
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=self.content)


def _patch_verdict(monkeypatch, verdict):
    def fake(llm, messages, schema, invoke_config=None):
        assert schema is SelfCheckVerdict
        assert invoke_config == {"tags": ["selfcheck_judge"]}
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
    monkeypatch.setattr(nodes, "_invoke_structured", fake)


def test_pass_verdict_keeps_state_untouched(monkeypatch):
    _patch_verdict(monkeypatch, SelfCheckVerdict(ok=True))
    llm = _RewriteLLM()
    assert nodes.self_check(_state(), llm) == {}
    assert llm.calls == 0


def test_fail_verdict_triggers_scoped_rewrite(monkeypatch):
    from langchain_core.messages import RemoveMessage
    _patch_verdict(monkeypatch, SelfCheckVerdict(
        ok=False, missing_conditions=["휴학 유형"], unsupported_claims=["최대 2년"]))
    llm = _RewriteLLM("경우를 나눈 답변")
    out = nodes.self_check(_state(), llm)
    # 재작성은 초안의 '대체' — 초안 제거 + 새 답변
    assert isinstance(out["messages"][0], RemoveMessage) and out["messages"][0].id == "d0"
    assert out["messages"][1].content == "경우를 나눈 답변"
    assert llm.calls == 1


def test_judge_exception_keeps_draft(monkeypatch):
    _patch_verdict(monkeypatch, RuntimeError("judge down"))
    assert nodes.self_check(_state(), _RewriteLLM()) == {}


def test_empty_rewrite_keeps_draft(monkeypatch):
    _patch_verdict(monkeypatch, SelfCheckVerdict(ok=False, missing_conditions=["학번"]))
    assert nodes.self_check(_state(), _RewriteLLM(content="  ")) == {}


def test_not_ok_without_findings_keeps_draft(monkeypatch):
    _patch_verdict(monkeypatch, SelfCheckVerdict(ok=False))
    llm = _RewriteLLM()
    assert nodes.self_check(_state(), llm) == {}
    assert llm.calls == 0


def test_no_evidence_skips_judge_entirely(monkeypatch):
    called = []
    monkeypatch.setattr(nodes, "_invoke_structured",
                        lambda *a, **k: called.append(1))
    assert nodes.self_check(_state(answers=[]), _RewriteLLM()) == {}
    assert not called


def test_rewrite_exception_keeps_draft(monkeypatch):
    _patch_verdict(monkeypatch, SelfCheckVerdict(ok=False, missing_conditions=["학번"]))
    class _Boom:
        def invoke(self, *a, **k):
            raise RuntimeError("rewrite down")
    assert nodes.self_check(_state(), _Boom()) == {}


def test_default_off(env_isolated):
    assert config.SELF_CHECK_ENABLED is False


def test_judge_sees_slot_supplement(monkeypatch):
    """보충 자료에서 온 사실이 '근거 없는 단정'으로 오판되지 않도록 judge 근거에 포함."""
    seen = {}
    def fake(llm, messages, schema, invoke_config=None):
        seen["input"] = messages[1].content
        return SelfCheckVerdict(ok=True)
    monkeypatch.setattr(nodes, "_invoke_structured", fake)
    st = _state(slot_supplement="[보충 검색 자료]\n일반휴학은 최대 2년.")
    nodes.self_check(st, _RewriteLLM())
    assert "일반휴학은 최대 2년" in seen["input"]


def test_verdict_schema_coerces_model_shapes():
    """UserSlots 선례(41c8f2a): 타입 불일치 하나가 판정 전체를 폐기하지 않는다."""
    v = SelfCheckVerdict.model_validate(
        {"ok": False, "unsupported_claims": "최대 2년", "missing_conditions": None})
    assert v.unsupported_claims == ["최대 2년"] and v.missing_conditions == []


def test_graph_topology_follows_lever(env_isolated, monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import importlib, sys

    class _LLM:
        def bind_tools(self, tools):
            return self

    from rag_agent.graph import create_agent_graph
    from rag_agent.tools import ToolFactory
    tools = ToolFactory(MagicMock()).create_tools()

    g_off = create_agent_graph(_LLM(), tools)
    assert "self_check" not in g_off.get_graph().nodes

    monkeypatch.setenv("SELF_CHECK_ENABLED", "true")
    importlib.reload(sys.modules["config"])
    g_on = create_agent_graph(_LLM(), tools)
    assert "self_check" in g_on.get_graph().nodes
