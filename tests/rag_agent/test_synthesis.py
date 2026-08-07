"""Unit tests for the issue-#126 generation-side levers.

Covers:
- edges._has_tool_evidence: marker/error outputs are not evidence; real chunks and
  compressed context summaries are.
- route_after_orchestrator_call: CLEAN_SYNTHESIS_ENABLED routing (default-off is
  behavior-neutral; enabled routes final answers with evidence to clean_synthesis).
- nodes._expand_parent_context: parent-id extraction, first-seen-order dedup,
  count/char caps, and graceful degradation on load failures (no silent crash of
  the answer path, every failure logged).
- nodes._build_synthesis_prompt_content: child evidence layout unchanged by default;
  parent expansion APPENDS (merge, not replace).
- nodes.clean_synthesis / fallback_response: single clean LLM call over the
  assembled context.
"""
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from rag_agent import edges, nodes


def _tool_msg(content: str, call_id: str = "tc0") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id)


CHUNK_A = (
    "Parent ID: guide_parent_3\n"
    "File Name: 학사안내.pdf\n"
    "Content: 수강신청 취소는 3/4선 이전까지 가능합니다."
)
CHUNK_B = (
    "Parent ID: guide_parent_7\n"
    "File Name: 학사안내.pdf\n"
    "Content: 계절수업은 별도 일정으로 운영됩니다."
)


class _FakeParentStore:
    """load_content stub: dict-backed, raises for ids marked broken."""

    def __init__(self, parents: dict, broken: set | None = None):
        self._parents = parents
        self._broken = broken or set()

    def load_content(self, parent_id: str) -> dict:
        if parent_id in self._broken:
            raise FileNotFoundError(f"missing parent file: {parent_id}")
        return self._parents[parent_id]


@pytest.fixture()
def parent_store(monkeypatch):
    """Install a fake parent store into nodes' lazy singleton slot."""

    def _install(parents: dict, broken: set | None = None):
        store = _FakeParentStore(parents, broken)
        monkeypatch.setattr(nodes, "_parent_store", store)
        return store

    yield _install
    monkeypatch.setattr(nodes, "_parent_store", None)


# ---------------------------------------------------------------------------
# edges._has_tool_evidence
# ---------------------------------------------------------------------------

class TestHasToolEvidence:
    def test_no_messages_no_summary_is_false(self):
        assert edges._has_tool_evidence({"messages": []}) is False

    def test_marker_outputs_are_not_evidence(self):
        state = {"messages": [_tool_msg("NO_RELEVANT_CHUNKS"), _tool_msg("NO_PARENT_DOCUMENT")]}
        assert edges._has_tool_evidence(state) is False

    def test_error_outputs_are_not_evidence(self):
        state = {"messages": [
            _tool_msg("RETRIEVAL_ERROR: search failed, see server log"),
            _tool_msg("PARENT_RETRIEVAL_ERROR: boom"),
        ]}
        assert edges._has_tool_evidence(state) is False

    def test_real_chunk_is_evidence(self):
        state = {"messages": [_tool_msg("NO_RELEVANT_CHUNKS"), _tool_msg(CHUNK_A)]}
        assert edges._has_tool_evidence(state) is True

    def test_context_summary_alone_is_evidence(self):
        """After compression the ToolMessages are gone — the summary carries the evidence."""
        state = {"messages": [], "context_summary": "### 학사안내.pdf\n- 취소는 3/4선 이전"}
        assert edges._has_tool_evidence(state) is True

    def test_whitespace_summary_is_not_evidence(self):
        state = {"messages": [], "context_summary": "   \n"}
        assert edges._has_tool_evidence(state) is False

    def test_non_tool_messages_ignored(self):
        state = {"messages": [HumanMessage(content=CHUNK_A), AIMessage(content=CHUNK_A)]}
        assert edges._has_tool_evidence(state) is False


# ---------------------------------------------------------------------------
# route_after_orchestrator_call — clean-synthesis routing
# ---------------------------------------------------------------------------

class TestCleanSynthesisRouting:
    def _final_answer_state(self, extra_msgs=None):
        """State whose last message is a final answer (no tool calls)."""
        msgs = list(extra_msgs or []) + [AIMessage(content="최종 답변")]
        return {"iteration_count": 1, "tool_call_count": 1, "messages": msgs}

    def test_default_off_keeps_collect_answer(self, monkeypatch):
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", False)
        state = self._final_answer_state([_tool_msg(CHUNK_A)])
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    def test_enabled_with_evidence_routes_to_clean_synthesis(self, monkeypatch):
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_MODE", "always")
        state = self._final_answer_state([_tool_msg(CHUNK_A)])
        assert edges.route_after_orchestrator_call(state) == "clean_synthesis"

    def test_enabled_without_evidence_keeps_collect_answer(self, monkeypatch):
        """Refusal path: no usable evidence → the orchestrator's own answer survives."""
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_MODE", "always")
        state = self._final_answer_state([_tool_msg("NO_RELEVANT_CHUNKS")])
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    # -- refusal_only mode (default; PR #144 conditional gate) --

    def _draft_state(self, draft: str, extra_msgs=None):
        msgs = list(extra_msgs or []) + [AIMessage(content=draft)]
        return {"iteration_count": 1, "tool_call_count": 1, "messages": msgs}

    def test_refusal_only_is_the_default_mode(self):
        assert config.CLEAN_SYNTHESIS_MODE == "refusal_only"

    def test_refusal_only_reroutes_refusal_draft(self, monkeypatch):
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_MODE", "refusal_only")
        state = self._draft_state("제공된 자료에는 해당 정보가 없습니다.", [_tool_msg(CHUNK_A)])
        assert edges.route_after_orchestrator_call(state) == "clean_synthesis"

    def test_refusal_only_keeps_non_refusal_draft(self, monkeypatch):
        """The −13 loss class of the 'always' A/B: a correct draft must stay untouched."""
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_MODE", "refusal_only")
        state = self._draft_state("수강 취소는 3/4선 이전까지 가능합니다.", [_tool_msg(CHUNK_A)])
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    def test_refusal_only_ignores_refusal_without_evidence(self, monkeypatch):
        """Out-of-scope refusal with no evidence stays a refusal (correct behavior)."""
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_MODE", "refusal_only")
        state = self._draft_state("제공된 자료에는 해당 정보가 없습니다.", [_tool_msg("NO_RELEVANT_CHUNKS")])
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    @pytest.mark.parametrize("draft", [
        "제공된 자료에는 해당 정보가 없습니다.",
        "제공된 자료에서 질문에 답할 수 있는 정보를 찾지 못했습니다.",
        "관련 정보가 없어 답변드리기 어렵습니다.",
        "해당 내용은 확인할 수 없습니다.",
    ])
    def test_refusal_pattern_matches_canonical_refusals(self, draft):
        assert edges._is_refusal_draft(AIMessage(content=draft)) is True

    @pytest.mark.parametrize("draft", [
        "수강 취소는 3/4선 이전까지 가능합니다.",
        "졸업학점은 130학점 이상 이수해야 합니다.",
        "",
    ])
    def test_refusal_pattern_ignores_normal_answers(self, draft):
        assert edges._is_refusal_draft(AIMessage(content=draft)) is False

    def test_enabled_does_not_touch_tools_route(self, monkeypatch):
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        msg = AIMessage(content="", tool_calls=[
            {"name": "search_child_chunks", "args": {"query": "q"}, "id": "tc1"}
        ])
        state = {"iteration_count": 1, "tool_call_count": 1,
                 "messages": [_tool_msg(CHUNK_A), msg]}
        assert edges.route_after_orchestrator_call(state) == "tools"

    def test_budget_exhausted_final_answer_is_still_adopted(self, monkeypatch):
        """PR #161: a ready final answer survives the budget boundary — it must not be
        discarded and re-synthesized by fallback_response (upstream 8b3e5ff0)."""
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        state = self._final_answer_state([_tool_msg(CHUNK_A)])
        state["iteration_count"] = config.MAX_ITERATIONS
        assert edges.route_after_orchestrator_call(state) == "collect_answer"

    def test_budget_exhausted_tool_request_falls_back(self, monkeypatch):
        """The budget still gates further tool execution: a pending tool request at
        the boundary goes to fallback_response, never to tools."""
        monkeypatch.setattr(config, "CLEAN_SYNTHESIS_ENABLED", True)
        msg = AIMessage(content="", tool_calls=[
            {"name": "search_child_chunks", "args": {"query": "q"}, "id": "tc1"}
        ])
        state = {"iteration_count": config.MAX_ITERATIONS, "tool_call_count": 1,
                 "messages": [_tool_msg(CHUNK_A), msg]}
        assert edges.route_after_orchestrator_call(state) == "fallback_response"


# ---------------------------------------------------------------------------
# nodes._expand_parent_context
# ---------------------------------------------------------------------------

def _parent(content: str, source: str = "학사안내.pdf") -> dict:
    return {"content": content, "metadata": {"source": source}}


class TestExpandParentContext:
    def test_no_parent_ids_returns_empty(self, parent_store):
        parent_store({})
        assert nodes._expand_parent_context(["no ids in here"]) == []

    def test_loads_parents_in_first_seen_order_deduped(self, parent_store):
        parent_store({
            "guide_parent_3": _parent("전문 A"),
            "guide_parent_7": _parent("전문 B"),
        })
        # CHUNK_A twice → its parent must appear once, before CHUNK_B's.
        blocks = nodes._expand_parent_context([CHUNK_A, CHUNK_A, CHUNK_B])
        assert len(blocks) == 2
        assert "guide_parent_3" in blocks[0] and "전문 A" in blocks[0]
        assert "guide_parent_7" in blocks[1] and "전문 B" in blocks[1]

    def test_max_parents_cap(self, parent_store, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_MAX_PARENTS", 1)
        parent_store({
            "guide_parent_3": _parent("전문 A"),
            "guide_parent_7": _parent("전문 B"),
        })
        blocks = nodes._expand_parent_context([CHUNK_A, CHUNK_B])
        assert len(blocks) == 1
        assert "guide_parent_3" in blocks[0]

    def test_max_chars_cap_keeps_first_parent(self, parent_store, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_MAX_CHARS", 200)
        parent_store({
            "guide_parent_3": _parent("가" * 150),
            "guide_parent_7": _parent("나" * 150),
        })
        blocks = nodes._expand_parent_context([CHUNK_A, CHUNK_B])
        # First block always kept (rank priority); second would exceed 200 chars.
        assert len(blocks) == 1

    def test_load_failure_is_skipped_and_logged(self, parent_store, caplog):
        parent_store(
            {"guide_parent_7": _parent("전문 B")},
            broken={"guide_parent_3"},
        )
        with caplog.at_level(logging.ERROR, logger="rag_agent.nodes"):
            blocks = nodes._expand_parent_context([CHUNK_A, CHUNK_B])
        assert len(blocks) == 1
        assert "전문 B" in blocks[0]
        assert any("guide_parent_3" in r.getMessage() for r in caplog.records)

    def test_empty_parent_content_is_skipped(self, parent_store):
        parent_store({
            "guide_parent_3": _parent("   "),
            "guide_parent_7": _parent("전문 B"),
        })
        blocks = nodes._expand_parent_context([CHUNK_A, CHUNK_B])
        assert len(blocks) == 1
        assert "전문 B" in blocks[0]

    def test_parent_already_fetched_by_agent_is_skipped(self, parent_store):
        """If the agent already pulled the parent via retrieve_parent_chunks, its full
        text is in a ToolMessage — expansion must not duplicate it."""
        full_text = "원문 전체 표 내용"
        parent_store({
            "guide_parent_3": _parent(full_text),
            "guide_parent_7": _parent("전문 B"),
        })
        # retrieve_parent_chunks output embeds the same stripped content.
        parent_tool_output = (
            f"Parent ID: guide_parent_3\nFile Name: 학사안내.pdf\nContent: {full_text}"
        )
        blocks = nodes._expand_parent_context([CHUNK_A, parent_tool_output, CHUNK_B])
        assert len(blocks) == 1
        assert "guide_parent_7" in blocks[0]

    def test_store_init_failure_degrades_to_no_expansion(self, monkeypatch, caplog):
        monkeypatch.setattr(nodes, "_parent_store", None)

        def _boom():
            raise RuntimeError("store path unavailable")

        monkeypatch.setattr(nodes, "_get_parent_store", _boom)
        with caplog.at_level(logging.ERROR, logger="rag_agent.nodes"):
            assert nodes._expand_parent_context([CHUNK_A]) == []
        assert any("parent store init failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# nodes._build_synthesis_prompt_content
# ---------------------------------------------------------------------------

class TestBuildSynthesisPromptContent:
    def test_no_evidence_placeholder(self, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        state = {"question": "질문?", "messages": []}
        content = nodes._build_synthesis_prompt_content(state)
        assert "문서에서 검색된 데이터가 없습니다." in content
        assert "사용자 질문: 질문?" in content

    def test_child_layout_matches_legacy_fallback(self, monkeypatch):
        """Expansion off → byte-identical to the pre-#126 fallback assembly."""
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        state = {
            "question": "질문?",
            "messages": [_tool_msg(CHUNK_A), _tool_msg(CHUNK_A, "tc1"), _tool_msg(CHUNK_B, "tc2")],
            "context_summary": "요약 내용",
        }
        content = nodes._build_synthesis_prompt_content(state)
        assert "## 압축된 조사 컨텍스트 (이전 반복)\n\n요약 내용" in content
        assert "--- 데이터 출처 1 ---\n" + CHUNK_A in content
        assert "--- 데이터 출처 2 ---\n" + CHUNK_B in content  # dedup: A appears once
        assert "--- 데이터 출처 3 ---" not in content
        assert "## 원문 맥락" not in content
        assert content.endswith("지시:\n위 데이터만 사용해 가능한 한 최선의 답변을 작성하세요.")

    def test_expansion_appends_parent_section(self, monkeypatch, parent_store):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", True)
        parent_store({"guide_parent_3": _parent("원문 전체 표"), "guide_parent_7": _parent("전문 B")})
        state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A)]}
        content = nodes._build_synthesis_prompt_content(state)
        # Merge, not replace: child snippet must survive alongside the parent section.
        assert CHUNK_A in content
        assert "## 원문 맥락 (검색된 조각의 상위 문단 전체)" in content
        assert "원문 전체 표" in content
        # Parent section comes AFTER the child data section.
        assert content.index("## 검색된 데이터") < content.index("## 원문 맥락")

    def test_expansion_without_tool_messages_adds_nothing(self, monkeypatch, parent_store):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", True)
        parent_store({})
        state = {"question": "질문?", "messages": [], "context_summary": "요약"}
        content = nodes._build_synthesis_prompt_content(state)
        assert "## 원문 맥락" not in content


# ---------------------------------------------------------------------------
# clean_synthesis / fallback_response nodes
# ---------------------------------------------------------------------------

class TestSynthesisNodes:
    def test_clean_synthesis_returns_llm_message(self, fake_llm, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A)]}
        result = nodes.clean_synthesis(state, fake_llm)
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "fake answer"

    def test_fallback_response_still_works(self, fake_llm, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A)]}
        result = nodes.fallback_response(state, fake_llm)
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "fake answer"

    def test_clean_synthesis_prompt_carries_evidence(self, monkeypatch):
        """The single-shot call must actually receive the collected chunks."""
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        captured = {}

        class _CapturingLLM:
            def invoke(self, messages, **kwargs):
                captured["messages"] = messages
                return AIMessage(content="ok")

        state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A)]}
        nodes.clean_synthesis(state, _CapturingLLM())
        human = captured["messages"][1]
        assert CHUNK_A in human.content
        assert "사용자 질문: 질문?" in human.content


def test_budget_marker_excluded_from_synthesis_and_compression(monkeypatch):
    """#89: 마커는 합성 프롬프트의 '검색된 데이터'에도, 압축 LLM 입력에도 안 들어간다."""
    monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
    marker = ToolMessage(content="SEARCH_BUDGET_EXCEEDED: 검색 시간 예산을 초과했습니다.",
                         tool_call_id="t9")
    state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A), marker]}
    content = nodes._build_synthesis_prompt_content(state)
    assert "SEARCH_BUDGET_EXCEEDED" not in content and CHUNK_A in content

    captured = {}
    class _LLM:
        def invoke(self, messages, **kwargs):
            captured["human"] = messages[1].content
            return AIMessage(content="요약")
    st = {"messages": [HumanMessage(content="q", id="m0"),
                       _tool_msg(CHUNK_A).model_copy(update={"id": "t1"}),
                       marker.model_copy(update={"id": "t2"})],
          "question": "q", "context_summary": "", "retrieval_keys": set()}
    nodes.compress_context(st, _LLM())
    assert "SEARCH_BUDGET_EXCEEDED" not in captured["human"]


# ---------------------------------------------------------------------------
# #177 P2 — parent expansion survives compress_context
# ---------------------------------------------------------------------------

class TestParentExpansionAfterCompress:
    def _msgs(self):
        return [
            HumanMessage(content="질문", id="m0"),
            _tool_msg(CHUNK_A).model_copy(update={"id": "t1"}),
            _tool_msg(CHUNK_B).model_copy(update={"id": "t2"}),
            _tool_msg(CHUNK_A).model_copy(update={"id": "t3"}),  # dup — dedup 대상
        ]

    def test_compress_harvests_parent_ids_first_seen_deduped(self, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", True)
        class _LLM:
            def invoke(self, messages, **kwargs):
                return AIMessage(content="요약")
        state = {"messages": self._msgs(), "question": "질문",
                 "context_summary": "", "retrieval_keys": set()}
        out = nodes.compress_context(state, _LLM())
        assert out["observed_parent_ids"] == ["guide_parent_3", "guide_parent_7"]

    def test_compress_merges_with_previous_harvest(self, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", True)
        class _LLM:
            def invoke(self, messages, **kwargs):
                return AIMessage(content="요약")
        state = {"messages": self._msgs(), "question": "질문", "context_summary": "이전",
                 "retrieval_keys": set(),
                 "observed_parent_ids": ["old_parent", "guide_parent_7"]}
        out = nodes.compress_context(state, _LLM())
        assert out["observed_parent_ids"] == ["old_parent", "guide_parent_7", "guide_parent_3"]

    def test_compress_lever_off_touches_no_state(self, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        class _LLM:
            def invoke(self, messages, **kwargs):
                return AIMessage(content="요약")
        state = {"messages": self._msgs(), "question": "질문",
                 "context_summary": "", "retrieval_keys": set()}
        assert "observed_parent_ids" not in nodes.compress_context(state, _LLM())

    def test_expansion_prior_ids_rank_after_current_and_dedup(self, parent_store):
        """현재 프롬프트에 실재하는 청크의 parent가 예산 우선(#126 실측 arm) — prior는 그 뒤."""
        parent_store({
            "guide_parent_3": {"content": "원문 A", "metadata": {"source": "학사안내.pdf"}},
            "guide_parent_7": {"content": "원문 B", "metadata": {"source": "학사안내.pdf"}},
        })
        blocks = nodes._expand_parent_context([CHUNK_A], prior_ids=["guide_parent_7", "guide_parent_3"])
        assert [b.splitlines()[0] for b in blocks] == [
            "--- 원문 (Parent ID: guide_parent_3 / File Name: 학사안내.pdf) ---",
            "--- 원문 (Parent ID: guide_parent_7 / File Name: 학사안내.pdf) ---",
        ]

    def test_na_placeholder_is_never_a_candidate(self, parent_store):
        parent_store({"guide_parent_3": {"content": "원문 A", "metadata": {"source": "학사안내.pdf"}}})
        blocks = nodes._expand_parent_context(
            ["Parent ID: n/a\nFile Name: x\nContent: y", CHUNK_A], prior_ids=["n/a"])
        assert len(blocks) == 1 and "guide_parent_3" in blocks[0]

    def test_synthesis_expands_from_observed_ids_after_compress(self, parent_store, monkeypatch):
        """압축 직후(현재 반복 ToolMessage 0건)에도 관찰된 parent가 확장된다 — P2의 표적."""
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", True)
        parent_store({"guide_parent_3": {"content": "원문 전체", "metadata": {"source": "학사안내.pdf"}}})
        state = {"question": "질문?", "messages": [], "context_summary": "압축 요약",
                 "observed_parent_ids": ["guide_parent_3"]}
        content = nodes._build_synthesis_prompt_content(state)
        assert "## 원문 맥락" in content and "원문 전체" in content

    def test_lever_off_ignores_observed_ids(self, parent_store, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        parent_store({"guide_parent_3": {"content": "원문 전체", "metadata": {"source": "학사안내.pdf"}}})
        state = {"question": "질문?", "messages": [], "context_summary": "압축 요약",
                 "observed_parent_ids": ["guide_parent_3"]}
        assert "## 원문 맥락" not in nodes._build_synthesis_prompt_content(state)


# ---------------------------------------------------------------------------
# #177 P1 — clean_synthesis 답변의 aggregation 재통과 우회
# ---------------------------------------------------------------------------

class TestCleanSynthesisAggregationBypass:
    def _state(self, answers):
        return {"agent_answers": answers, "originalQuery": "질문", "userSlots": {}}

    def _ans(self, idx, text, clean):
        return {"index": idx, "question": "q", "answer": text, "clean": clean}

    def test_single_clean_answer_bypasses_aggregation_llm(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        class _LLM:
            def invoke(self, *a, **k):
                raise AssertionError("aggregation LLM must not be called on bypass")
        out = nodes.aggregate_answers(self._state([self._ans(0, "클린 답", True)]), _LLM())
        assert out["messages"][0].content == "클린 답"

    def test_non_clean_answer_still_aggregates(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        calls = []
        class _LLM:
            def invoke(self, messages, **kwargs):
                calls.append(messages)
                return AIMessage(content="합성")
        out = nodes.aggregate_answers(self._state([self._ans(0, "초안", False)]), _LLM())
        assert calls and out["messages"][0].content == "합성"

    def test_multi_answers_still_aggregate_even_if_clean(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", False)
        calls = []
        class _LLM:
            def invoke(self, messages, **kwargs):
                calls.append(messages)
                return AIMessage(content="합성")
        answers = [self._ans(0, "답1", True), self._ans(1, "답2", True)]
        nodes.aggregate_answers(self._state(answers), _LLM())
        assert calls

    def test_slot_lever_on_disables_bypass(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(config, "SLOT_CLARIFY_ENABLED", False)
        monkeypatch.setattr(config, "SLOT_SEARCH_ENABLED", False)
        calls = []
        class _LLM:
            def invoke(self, messages, **kwargs):
                calls.append(messages)
                return AIMessage(content="합성")
        nodes.aggregate_answers(self._state([self._ans(0, "클린 답", True)]), _LLM())
        assert calls

    def test_collect_answer_marks_clean_only_when_valid(self):
        valid = {"messages": [AIMessage(content="답")], "question_index": 0,
                 "question": "q", "clean_synthesized": True}
        assert nodes.collect_answer(valid)["agent_answers"][0]["clean"] is True
        invalid = {"messages": [AIMessage(content="")], "question_index": 0,
                   "question": "q", "clean_synthesized": True}
        assert nodes.collect_answer(invalid)["agent_answers"][0]["clean"] is False

    def test_clean_synthesis_sets_state_flag(self, fake_llm, monkeypatch):
        monkeypatch.setattr(config, "PARENT_EXPANSION_ENABLED", False)
        state = {"question": "질문?", "messages": [_tool_msg(CHUNK_A)]}
        assert nodes.clean_synthesis(state, fake_llm)["clean_synthesized"] is True
