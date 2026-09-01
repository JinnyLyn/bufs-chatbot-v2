import logging
import re
import time
from typing import List, Literal, Optional, Set
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage, ToolMessage
from langgraph.types import Command
from .graph_state import State, AgentState
from .schemas import QueryAnalysis, SelfCheckVerdict, UserSlots
from .prompts import *
import config
from utils import estimate_context_tokens
from config import BASE_TOKEN_THRESHOLD, TOKEN_GROWTH_FACTOR

logger = logging.getLogger(__name__)

# Lazily-built singleton so importing nodes.py never touches the filesystem
# (ParentStoreManager.__init__ mkdirs the store path).
_parent_store = None


def _get_parent_store():
    global _parent_store
    if _parent_store is None:
        from db.parent_store_manager import ParentStoreManager
        _parent_store = ParentStoreManager()
    return _parent_store


def _invoke_structured(llm, messages, schema, invoke_config=None):
    """Invoke with_structured_output(schema), trying methods until one parses.

    Ollama models support different structured-output mechanisms (qwen3.5:9b →
    function_calling; qwen3:4b-instruct → json_schema/default), so fall back across
    them rather than hard-coding one. ``invoke_config`` is passed through to invoke()
    (e.g. tags that let the streaming layer filter internal calls, #176).
    """
    # NOTE: .with_config(temperature=…) is a no-op in langchain-ollama 1.1.0 (sampling options
    # are read only from the ChatOllama constructor), so it was dropped — the global temperature
    # from RAGSystem applies. Pin a per-call temperature via the constructor if ever needed.
    base = llm
    cfg = getattr(config, "STRUCTURED_OUTPUT_METHOD", "auto")
    methods = [cfg] if cfg and cfg != "auto" else ["function_calling", "json_schema", None]
    last_exc = None
    for method in methods:
        try:
            structured = (
                base.with_structured_output(schema)
                if method is None
                else base.with_structured_output(schema, method=method)
            )
            if invoke_config is None:
                return structured.invoke(messages)
            return structured.invoke(messages, config=invoke_config)
        except Exception as exc:  # parse / validation error → try the next method
            last_exc = exc
            logger.debug("structured output method=%r failed (%s) — trying next", method, exc)
    raise last_exc


def _invoke_structured_rewrite(llm, messages):
    """Backward-compatible wrapper (QueryAnalysis) — see _invoke_structured."""
    return _invoke_structured(llm, messages, QueryAnalysis)


# --- User-slot extraction (issue #145 처방 1) ---

_SLOT_LABELS = [
    ("admission_year", "학번/입학년도"),
    ("grade", "학년"),
    ("semester", "대상 학기"),
    ("status", "학적 신분"),
    ("major", "전공/학과"),
    ("credits", "이수학점/평점"),
    ("leave_type", "휴학 유형"),
]


def format_user_slots(slots: dict) -> str:
    """Render extracted slots as the deterministic '[사용자 상황 조건]' block.

    Returns "" when nothing was extracted — the empty string is the scoping contract:
    no block → no injection → the question's trajectory is byte-identical to OFF.
    """
    if not slots:
        return ""
    lines = [
        f"- {label}: {str(slots.get(key)).strip()}"
        for key, label in _SLOT_LABELS
        if str(slots.get(key) or "").strip()
    ]
    lines += [
        f"- 기타 조건: {str(x).strip()}"
        for x in (slots.get("extra") or [])
        if str(x or "").strip()
    ]
    if not lines:
        return ""
    return "[사용자 상황 조건]\n" + "\n".join(lines)


def _slot_search_queries(question: str, slots: dict) -> List[str]:
    """Deterministic slot-derived search queries (issue #145 슬롯 기반 2차 검색).

    One query per condition term, in a fixed order: stated slot values (_SLOT_LABELS
    order) → extra conditions → required-condition names. Deduped, capped at
    SLOT_SEARCH_MAX_QUERIES. Empty list when there is nothing to search on — the
    scoping contract (no terms → no supplementary block → OFF-identical input).
    """
    terms: List[str] = [
        str(slots.get(key)).strip()
        for key, _ in _SLOT_LABELS
        if str(slots.get(key) or "").strip()
    ]
    terms += [str(x).strip() for x in (slots.get("extra") or []) if str(x or "").strip()]
    terms += [str(c).strip() for c in (slots.get("required_conditions") or []) if str(c or "").strip()]
    unique_terms = list(dict.fromkeys(terms))[: config.SLOT_SEARCH_MAX_QUERIES]
    q = (question or "").strip()
    return [f"{q} {t}".strip() for t in unique_terms]


def _slot_secondary_search(collection, question: str, slots: dict) -> str:
    """Run the slot-derived queries against the child collection and format the hits
    as a supplementary-evidence block (same Parent ID/File Name/Content layout as the
    retrieval tool, so the LLM sees one consistent evidence format).

    Purely additive and code-driven: the agent's own retrieval already happened and is
    untouched. Any failure degrades to fewer (or zero) hits — logged, never fatal.
    """
    queries = _slot_search_queries(question, slots)
    if not queries or collection is None:
        return ""
    blocks: List[str] = []
    seen_contents = set()
    for query in queries:
        try:
            results = collection.similarity_search(
                query, k=config.SLOT_SEARCH_LIMIT, score_threshold=config.SEARCH_SCORE_THRESHOLD)
        except Exception:
            logger.exception("slot secondary search failed for query=%r — skipping", query)
            continue
        for doc in results:
            content = (doc.page_content or "").strip()
            if not content or content in seen_contents:
                continue
            seen_contents.add(content)
            blocks.append(
                f"Parent ID: {doc.metadata.get('parent_id', '')}\n"
                f"File Name: {doc.metadata.get('source', '')}\n"
                f"Content: {content}"
            )
    if not blocks:
        return ""
    return (
        "[보충 검색 자료 — 사용자 조건 기반 추가 검색]\n\n"
        + "\n\n".join(f"--- 보충 {i} ---\n{b}" for i, b in enumerate(blocks, 1))
    )


def extract_user_slots(llm, question: str) -> dict:
    """Extract explicitly-stated user conditions from the question (one structured call).

    Any failure degrades to {} — the pipeline must never die on the extractor, but the
    failure is logged so it stays traceable (no silent errors).
    """
    if not (question or "").strip():
        return {}
    try:
        result = _invoke_structured(
            llm,
            [SystemMessage(content=get_slot_extraction_prompt()), HumanMessage(content=question)],
            UserSlots,
        )
        return result.model_dump() if result is not None else {}
    except Exception:
        logger.exception("slot extraction failed — continuing without user slots")
        return {}

def summarize_history(state: State, llm):
    if len(state["messages"]) < 4:
        return {"conversation_summary": ""}
    
    relevant_msgs = [
        msg for msg in state["messages"][:-1]
        if isinstance(msg, (HumanMessage, AIMessage)) and not getattr(msg, "tool_calls", None)
    ]

    if not relevant_msgs:
        return {"conversation_summary": ""}
    
    conversation = "대화 기록:\n"
    for msg in relevant_msgs[-6:]:
        role = "사용자" if isinstance(msg, HumanMessage) else "어시스턴트"
        conversation += f"{role}: {msg.content}\n"

    summary_response = llm.invoke([SystemMessage(content=get_conversation_summary_prompt()), HumanMessage(content=conversation)])  # temperature override removed: .with_config no-op (see _invoke_structured_rewrite)
    return {"conversation_summary": summary_response.content, "agent_answers": [{"__reset__": True}]}

def rewrite_query(state: State, llm):
    last_message = state["messages"][-1]
    conversation_summary = state.get("conversation_summary", "")

    # Issue #145 처방 1: extract explicitly-stated user conditions from the ORIGINAL
    # question (pre-rewrite surface form) so the generation stage can apply them.
    # {} when disabled or nothing stated → downstream injections are no-ops.
    user_slots = extract_user_slots(llm, last_message.content) if config.SLOT_EXTRACTION_ENABLED else {}

    # Issue #15 A/B switch: when rewriting is disabled, pass the original question straight to
    # the agent as the single search query (no LLM rewrite, no clarify detour). bge-m3 already
    # handles Korean well, so the raw surface form often retrieves better than a rewrite.
    if not config.REWRITE_ENABLED:
        delete_all = [RemoveMessage(id=m.id) for m in state["messages"] if not isinstance(m, SystemMessage)]
        return {"questionIsClear": True, "messages": delete_all,
                "originalQuery": last_message.content, "rewrittenQuestions": [last_message.content],
                "userSlots": user_slots}

    context_section = (f"대화 요약:\n{conversation_summary}\n" if conversation_summary.strip() else "") + f"사용자 질문:\n{last_message.content}\n"

    response = _invoke_structured_rewrite(
        llm, [SystemMessage(content=get_rewrite_query_prompt()), HumanMessage(content=context_section)]
    )

    if response.questions and response.is_clear:
        delete_all = [RemoveMessage(id=m.id) for m in state["messages"] if not isinstance(m, SystemMessage)]
        return {"questionIsClear": True, "messages": delete_all, "originalQuery": last_message.content,
                "rewrittenQuestions": response.questions, "userSlots": user_slots}

    clarification = response.clarification_needed if response.clarification_needed and len(response.clarification_needed.strip()) > 10 else "질문을 정확히 이해하려면 추가 정보가 필요합니다."
    return {"questionIsClear": False, "messages": [AIMessage(content=clarification)]}

def request_clarification(state: State):
    return {}

# --- Agent Nodes ---
def orchestrator(state: AgentState, llm_with_tools):
    context_summary = state.get("context_summary", "").strip()
    sys_msg = SystemMessage(content=get_orchestrator_prompt())
    summary_injection = (
        [HumanMessage(content=f"[이전 조사에서 압축된 컨텍스트]\n\n{context_summary}")]
        if context_summary else []
    )
    # Issue #145 처방 1 — v3: NO orchestrator-side slot injection. Both in-loop variants
    # regressed on the S0-matched A/B: v1(전체 지시) refusals 33→50, v2(적용 지시만)
    # refusals 33→43 AND doc_hit 76→70 — a condition block inside the agent loop pushes
    # qwen3.5:9b toward hedging and perturbs its search-query formulation. Slots are
    # applied at aggregate_answers only (see below), which by construction cannot touch
    # retrieval or the agent trajectory.
    if not state.get("messages"):
        # #89: arm the elapsed-budget reference BEFORE the first LLM turn so that turn's
        # latency counts against the budget. State-only and gated, so the lever-off path's
        # state stays byte-identical.
        started_at = time.monotonic() if config.TOOL_CALL_SOFT_TIMEOUT_S > 0 else None
        human_msg = HumanMessage(content=state["question"])
        force_search = HumanMessage(content=get_force_search_instruction())
        response = llm_with_tools.invoke([sys_msg] + summary_injection + [human_msg, force_search])
        update = {"messages": [human_msg, response], "tool_call_count": len(response.tool_calls or []), "iteration_count": 1}
        if started_at is not None:
            update["loop_started_at"] = started_at
        return update

    response = llm_with_tools.invoke([sys_msg] + summary_injection + state["messages"])
    tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []
    return {"messages": [response], "tool_call_count": len(tool_calls) if tool_calls else 0, "iteration_count": 1}

def _collect_unique_tool_contents(state: AgentState) -> List[str]:
    """Unique ToolMessage contents in first-seen order (the agent's actual evidence)."""
    seen = set()
    unique_contents = []
    for m in state["messages"]:
        if isinstance(m, ToolMessage) and m.content not in seen:
            # #89: the budget marker is an instruction to the orchestrator, not evidence —
            # rendering it under "## 검색된 데이터" would leak it into the answer prompt.
            if str(m.content or "").startswith("SEARCH_BUDGET_EXCEEDED"):
                continue
            unique_contents.append(m.content)
            seen.add(m.content)
    return unique_contents


# Matches the "Parent ID: <id>" line the retrieval tools prepend to every chunk block
# (see ToolFactory._search_child_chunks / _retrieve_parent_chunks output format).
_PARENT_ID_LINE = re.compile(r"(?m)^Parent ID:\s*(.+?)\s*$")


def _expand_parent_context(unique_contents: List[str],
                           prior_ids: Optional[List[str]] = None,
                           question: str = "") -> List[str]:
    """Auto parent expansion (issue #126): load the parent originals for the child chunks
    the agent actually saw.

    ``prior_ids`` (#177 P2) carries parent ids observed in iterations whose ToolMessages
    compress_context has since deleted. They rank AFTER the current-iteration ids: the
    chunks actually present in the prompt keep budget priority (the #126 measured arm),
    and compressed-away parents only fill whatever the MAX_PARENTS/MAX_CHARS caps have
    left. Returns formatted parent blocks, deduped by parent_id ("n/a" placeholders
    skipped), capped at PARENT_EXPANSION_MAX_PARENTS / PARENT_EXPANSION_MAX_CHARS.
    Any failure degrades to fewer (or zero) blocks — the answer path must never die on
    an expansion error, but every failure is logged with the offending parent_id so it
    stays traceable.
    """
    ordered_ids: List[str] = []
    seen_ids = set()

    def _admit(pid: str) -> None:
        # "n/a" is the tools' placeholder for a parentless block — not a loadable id.
        if pid and pid != "n/a" and pid not in seen_ids:
            seen_ids.add(pid)
            ordered_ids.append(pid)

    for content in unique_contents:
        for pid in _PARENT_ID_LINE.findall(str(content or "")):
            _admit(pid)
    for pid in prior_ids or []:
        _admit(pid)

    if not ordered_ids:
        return []

    try:
        store = _get_parent_store()
    except Exception:
        logger.exception("parent expansion disabled for this call: parent store init failed")
        return []

    from rag_agent import ocu as _ocu

    # Same stand-down rule as retrieval (tools._demotion_predicate): an OCU question
    # gets its OCU parents. Empty question (defensive) also stands down — fail-open.
    ocu_scope_armed = (
        config.OCU_FILTER_ENABLED and bool(question) and not _ocu.is_ocu_question(question)
    )

    blocks: List[str] = []
    total_chars = 0
    for pid in ordered_ids:
        if len(blocks) >= config.PARENT_EXPANSION_MAX_PARENTS:
            break
        try:
            parent = store.load_content(pid)
        except Exception:
            # Missing/corrupt parent file must not kill the answer — skip it, keep the trace.
            logger.exception("parent expansion: failed to load parent_id=%s", pid)
            continue
        content = (parent.get("content") or "").strip()
        if not content:
            logger.warning("parent expansion: empty content for parent_id=%s — skipped", pid)
            continue
        if any(content in uc for uc in unique_contents):
            # The agent already fetched this parent's full text via retrieve_parent_chunks
            # (tool output embeds the same stripped content) — appending it again would
            # only duplicate large context.
            continue
        if ocu_scope_armed and _ocu.is_ocu_parent(content):
            # Without this, a readmitted OCU child (demote-never-delete) or a replayed
            # observed_parent_id re-imports the full OCU block into the synthesis prompt
            # and reproduces the contamination retrieval-side demotion just removed.
            # The child evidence itself stays in the prompt — only the parent append is
            # skipped, so an only-evidence answer can still cite what was retrieved.
            logger.info("parent expansion: OCU scope skipped parent_id=%s", pid)
            continue
        source = (parent.get("metadata") or {}).get("source", "unknown")
        block = f"--- 원문 (Parent ID: {pid} / File Name: {source}) ---\n{content}"
        # The first parent is always kept even past the char cap (rank priority; with
        # MAX_PARENT_SIZE=6000 a single parent fits the default 9000 budget anyway).
        if blocks and total_chars + len(block) > config.PARENT_EXPANSION_MAX_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
    return blocks


def _build_synthesis_prompt_content(state: AgentState) -> str:
    """Assemble the answer-from-context user prompt shared by fallback_response and
    clean_synthesis. Layout for the child-evidence part is byte-identical to the
    pre-#126 fallback_response; parent expansion only APPENDS a section (merge, not
    replace — #126's naive full-replacement arm regressed 4 needle-in-haystack cases)."""
    unique_contents = _collect_unique_tool_contents(state)
    context_summary = state.get("context_summary", "").strip()

    context_parts = []
    if context_summary:
        context_parts.append(f"## 압축된 조사 컨텍스트 (이전 반복)\n\n{context_summary}")
    if unique_contents:
        context_parts.append(
            "## 검색된 데이터 (현재 반복)\n\n" +
            "\n\n".join(f"--- 데이터 출처 {i} ---\n{content}" for i, content in enumerate(unique_contents, 1))
        )

    if config.PARENT_EXPANSION_ENABLED:
        # #177 P2: after compress_context the current-iteration ToolMessages may be empty
        # or missing earlier evidence — observed_parent_ids preserves those candidates,
        # so the compressed multi-turn population (the lever's target) still expands.
        prior_ids = state.get("observed_parent_ids") or []
        if unique_contents or prior_ids:
            parent_blocks = _expand_parent_context(
                unique_contents, prior_ids, question=state.get("question") or "")
            if parent_blocks:
                context_parts.append(
                    "## 원문 맥락 (검색된 조각의 상위 문단 전체)\n\n" + "\n\n".join(parent_blocks)
                )

    context_text = "\n\n".join(context_parts) if context_parts else "문서에서 검색된 데이터가 없습니다."

    return (
        f"사용자 질문: {state.get('question')}\n\n"
        f"{context_text}\n\n"
        f"{get_fallback_task_instruction()}"
    )


def fallback_response(state: AgentState, llm):
    prompt_content = _build_synthesis_prompt_content(state)
    response = llm.invoke([SystemMessage(content=get_fallback_response_prompt()), HumanMessage(content=prompt_content)])
    return {"messages": [response]}


def clean_synthesis(state: AgentState, llm):
    """Final clean synthesis (issue #126): one single-shot answer-from-context call over the
    evidence the agent collected, replacing the orchestrator's in-loop draft answer.

    #126's simulation showed 1/3 of live generation failures (13/40) pass when the same
    chunks are given to exactly this call (fallback prompt + task instruction, temp0) —
    the multi-turn agent loop, not the context, is what loses them. Reuses the fallback
    prompt/assembly verbatim so live behavior matches the measured arm. Only routed to
    when usable tool evidence exists (see route_after_orchestrator_call), so refusal
    paths keep the orchestrator's own answer.
    """
    prompt_content = _build_synthesis_prompt_content(state)
    response = llm.invoke([SystemMessage(content=get_fallback_response_prompt()), HumanMessage(content=prompt_content)])
    # #177 P1: mark the answer as an already-final synthesis so aggregate_answers can
    # skip the degrading LLM re-pass when it would add no information.
    return {"messages": [response], "clean_synthesized": True}

def should_compress_context(state: AgentState) -> Command[Literal["compress_context", "orchestrator"]]:
    messages = state["messages"]

    new_ids: Set[str] = set()
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc["name"] == "retrieve_parent_chunks":
                    raw = tc["args"].get("parent_id") or tc["args"].get("id") or tc["args"].get("ids") or []
                    if isinstance(raw, str):
                        new_ids.add(f"parent::{raw}")
                    else:
                        new_ids.update(f"parent::{r}" for r in raw)

                elif tc["name"] == "search_child_chunks":
                    query = tc["args"].get("query", "")
                    if query:
                        new_ids.add(f"search::{query}")
            break

    updated_ids = state.get("retrieval_keys", set()) | new_ids

    current_token_messages = estimate_context_tokens(messages)
    current_token_summary = estimate_context_tokens([HumanMessage(content=state.get("context_summary", ""))])
    current_tokens = current_token_messages + current_token_summary

    max_allowed = BASE_TOKEN_THRESHOLD + int(current_token_summary * TOKEN_GROWTH_FACTOR)

    goto = "compress_context" if current_tokens > max_allowed else "orchestrator"
    return Command(update={"retrieval_keys": updated_ids}, goto=goto)

def compress_context(state: AgentState, llm):
    messages = state["messages"]
    existing_summary = state.get("context_summary", "").strip()

    if not messages:
        return {}

    conversation_text = f"사용자 질문:\n{state.get('question')}\n\n압축할 대화:\n\n"
    if existing_summary:
        conversation_text += f"[이전 압축 컨텍스트]\n{existing_summary}\n\n"

    for msg in messages[1:]:
        if isinstance(msg, AIMessage):
            tool_calls_info = ""
            if getattr(msg, "tool_calls", None):
                calls = ", ".join(f"{tc['name']}({tc['args']})" for tc in msg.tool_calls)
                tool_calls_info = f" | 도구 호출: {calls}"
            conversation_text += f"[어시스턴트{tool_calls_info}]\n{msg.content or '(도구 호출만 있음)'}\n\n"
        elif isinstance(msg, ToolMessage):
            # #89: keep the budget marker out of the compression LLM — it would be baked
            # into context_summary as a "search failure" and re-injected every later turn.
            if str(msg.content or "").startswith("SEARCH_BUDGET_EXCEEDED"):
                continue
            tool_name = getattr(msg, "name", "tool")
            conversation_text += f"[도구 결과 - {tool_name}]\n{msg.content}\n\n"

    summary_response = llm.invoke([SystemMessage(content=get_context_compression_prompt()), HumanMessage(content=conversation_text)])
    new_summary = summary_response.content

    retrieved_ids: Set[str] = state.get("retrieval_keys", set())
    if retrieved_ids:
        parent_ids = sorted(r for r in retrieved_ids if r.startswith("parent::"))
        search_queries = sorted(r.replace("search::", "") for r in retrieved_ids if r.startswith("search::"))

        block = "\n\n---\n**이미 수행됨 (반복하지 말 것):**\n"
        if parent_ids:
            block += "이미 가져온 parent chunks:\n" + "\n".join(f"- {p.replace('parent::', '')}" for p in parent_ids) + "\n"
        if search_queries:
            block += "이미 실행한 검색 쿼리:\n" + "\n".join(f"- {q}" for q in search_queries) + "\n"
        new_summary += block

    updates = {"context_summary": new_summary,
               "messages": [RemoveMessage(id=m.id) for m in messages[1:]]}

    # #177 P2: the ToolMessages being removed are the only carrier of "Parent ID:" lines.
    # Harvest them (first-seen order, deduped against earlier harvests) so parent expansion
    # at synthesis time still knows what the agent saw. State-only: no prompt input changes;
    # gated so the OFF path's state stays byte-identical too.
    if config.PARENT_EXPANSION_ENABLED:
        observed = list(state.get("observed_parent_ids") or [])
        seen_pids = set(observed)
        for msg in messages[1:]:
            if isinstance(msg, ToolMessage):
                for pid in _PARENT_ID_LINE.findall(str(msg.content or "")):
                    if pid and pid != "n/a" and pid not in seen_pids:
                        seen_pids.add(pid)
                        observed.append(pid)
        updates["observed_parent_ids"] = observed

    return updates

def collect_answer(state: AgentState):
    last_message = state["messages"][-1]
    is_valid = isinstance(last_message, AIMessage) and last_message.content and not last_message.tool_calls
    answer = last_message.content if is_valid else "답변을 생성하지 못했습니다."
    return {
        "final_answer": answer,
        "agent_answers": [{"index": state["question_index"], "question": state["question"], "answer": answer,
                           # #177 P1: only a VALID clean-synthesis answer earns the bypass.
                           "clean": bool(state.get("clean_synthesized") and is_valid)}]
    }
# --- End of Agent Nodes---

def aggregate_answers(state: State, llm, collection=None):
    if not state.get("agent_answers"):
        return {"messages": [AIMessage(content="생성된 답변이 없습니다.")]}

    sorted_answers = sorted(state["agent_answers"], key=lambda x: x["index"])

    formatted_answers = ""
    for i, ans in enumerate(sorted_answers, start=1):
        formatted_answers += (f"\n답변 {i}:\n"f"{ans['answer']}\n")

    content = f"""원래 사용자 질문: {state["originalQuery"]}\n검색된 답변:{formatted_answers}"""

    # Issue #145 처방 1: final condition-coverage check. Only appended when slots exist,
    # so slot-free questions aggregate on byte-identical input.
    slots = state.get("userSlots") or {}
    slot_text = format_user_slots(slots) if config.SLOT_EXTRACTION_ENABLED else ""

    # Codex P1 (#177): a clean_synthesis answer is already a final single-shot synthesis
    # over the full collected evidence — re-passing it through the aggregation LLM can
    # only degrade it (the same degradation class refusal_only mode exists to prevent:
    # PR #144 measured −13 on re-synthesized already-correct drafts). Bypass when the
    # re-pass would add no information: single answer, no slot lever active. Multi-question
    # merging and every slot/clarify/supplement injection still aggregate as before.
    if (len(sorted_answers) == 1 and sorted_answers[0].get("clean")
            and not config.SLOT_EXTRACTION_ENABLED):
        return {"messages": [AIMessage(content=sorted_answers[0]["answer"])]}

    if slot_text:
        # v2 wording: the v1 "없으면 그 사실을 명시하세요" clause invited refusal-style
        # hedging (S1 A/B refusals +17) — keep only the condition-application instruction.
        content += (
            f"\n\n{slot_text}\n\n"
            "조건에 따라 규정이 달라지는 부분은 사용자의 조건 기준으로 답하세요."
        )

    # Issue #145 처방 2 (슬롯-clarify): the extractor flagged conditions the answer depends
    # on that the user did NOT state. Instruct a CONDITIONAL answer (content preserved —
    # a hard stop-and-ask would repeat issue #51's false-clarification regression) plus one
    # closing sentence asking for the missing conditions. Empty list → no injection.
    missing = [
        str(c).strip()
        for c in (slots.get("required_conditions") or [])
        if str(c or "").strip()
    ] if (config.SLOT_EXTRACTION_ENABLED and config.SLOT_CLARIFY_ENABLED) else []
    if missing:
        content += (
            f"\n\n[확인 필요 조건] 정확한 답에 필요하지만 질문에 없는 정보: {', '.join(missing)}\n\n"
            "지시: 검색된 답변의 내용은 빠짐없이 유지하되, 위 조건에 따라 규정이 달라지는 부분은 "
            "경우를 나눠 답하세요(예: 일반휴학이면 …, 병역휴학이면 …). 답변 마지막에 위 정보를 "
            "알려주시면 더 정확한 안내가 가능하다는 요청을 한 문장으로만 덧붙이세요. "
            "확인 요청을 이유로 본문 내용을 생략하거나 답변을 거부하지 마세요."
        )

    # Issue #145 슬롯 기반 2차 검색: code-driven supplementary retrieval on the condition
    # terms — feeds the clarify lever the per-case rule chunks it is asked to split on.
    # Empty block (no terms / disabled / no collection / no hits) → OFF-identical input.
    supplement = ""
    if config.SLOT_EXTRACTION_ENABLED and config.SLOT_SEARCH_ENABLED:
        supplement = _slot_secondary_search(collection, state.get("originalQuery", ""), slots) or ""
        if supplement:
            content += (
                f"\n\n{supplement}\n\n"
                "지시: 위 보충 자료에 근거한 사실은 답변에 사용할 수 있습니다. "
                "보충 자료와 검색된 답변 어디에도 없는 내용은 추가하지 마세요."
            )

    user_message = HumanMessage(content=content)
    synthesis_response = llm.invoke([SystemMessage(content=get_aggregation_prompt()), user_message])
    result = {"messages": [AIMessage(content=synthesis_response.content)]}
    if config.SELF_CHECK_ENABLED and supplement:
        # #176: 자가검사 judge가 초안과 같은 근거를 보게 한다 — 보충 자료를 빼면
        # 보충에서 온 올바른 사실이 '근거 없는 단정'으로 오판된다. 레버 OFF면 미기록.
        result["slot_supplement"] = supplement
    return result


def self_check(state: State, llm):
    """답변 전 자가검사 (#176 = #145 처방 4). Post-aggregation, default OFF.

    JUDGE(구조화 판정) → PASS면 state 무변경(집계 답변 바이트 그대로) → FAIL일 때만
    지적사항-스코프 REWRITE 1회. 판정 실패·빈 재작성 등 모든 예외는 초안 유지로
    강등 — 이 노드는 답변을 잃게 만들 수 없다. JUDGE 호출은 "selfcheck_judge" 태그를
    달아 스트리밍 계층이 내부 토큰을 사용자 답변으로 내보내지 않게 한다.
    """
    draft, draft_id = "", None
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            draft = str(msg.content)
            draft_id = getattr(msg, "id", None)
            break

    answers = sorted(state.get("agent_answers") or [], key=lambda x: x["index"])
    evidence = "\n".join(f"\n답변 {i}:\n{a['answer']}" for i, a in enumerate(answers, start=1))
    # 집계가 초안에 쓴 근거를 그대로 재현해야 한다: 슬롯 2차 검색 보충 자료를 빼면
    # 보충에서 온 올바른 사실이 '근거 없는 단정'으로 오판·삭제된다.
    supplement = str(state.get("slot_supplement") or "").strip()
    if supplement:
        evidence += f"\n\n{supplement}"
    if not draft.strip() or not evidence.strip():
        return {}

    judge_input = (
        f"사용자 질문: {state.get('originalQuery', '')}\n\n"
        f"근거 자료 (검색된 답변들):{evidence}\n\n"
        f"최종 답변 초안:\n{draft}"
    )
    # 판정 결과의 필드 접근까지 try 안에 둔다 (extract_user_slots의 model_dump와 같은
    # 이유): 비정상 반환형이 노드 밖으로 새면 그래프 예외 → 답변 유실.
    try:
        verdict = _invoke_structured(
            llm,
            [SystemMessage(content=get_self_check_prompt()), HumanMessage(content=judge_input)],
            SelfCheckVerdict,
            invoke_config={"tags": ["selfcheck_judge"]},
        )
        if verdict is None or verdict.ok:
            return {}
        unsupported = [str(c).strip() for c in (verdict.unsupported_claims or []) if str(c or "").strip()]
        missing = [str(c).strip() for c in (verdict.missing_conditions or []) if str(c or "").strip()]
    except Exception:
        logger.exception("self-check judge failed — keeping the aggregated answer")
        return {}

    findings = ""
    if unsupported:
        findings += "근거 없는 단정:\n" + "\n".join(f"- {c}" for c in unsupported) + "\n"
    if missing:
        findings += "조건 무시 단정 (질문에 없는 조건):\n" + "\n".join(f"- {c}" for c in missing) + "\n"
    if not findings:
        # ok=False인데 지적사항이 비어 있으면 재작성할 대상이 없다 — 초안 유지.
        logger.warning("self-check verdict not ok but no findings — keeping the draft")
        return {}
    if missing:
        # 되묻기 한 문장은 실제로 빠진 조건이 있을 때만 — 무조건 헤징은 #51이 잰 비용.
        findings += ("\n추가 지시: 답변 마지막에 위 조건을 알려주면 더 정확한 안내가 "
                     "가능하다는 요청을 한 문장으로만 덧붙이세요.\n")

    try:
        response = llm.invoke([
            SystemMessage(content=get_self_check_rewrite_prompt()),
            HumanMessage(content=f"{judge_input}\n\n지적사항:\n{findings}"),
        ])
    except Exception:
        logger.exception("self-check rewrite failed — keeping the aggregated answer")
        return {}
    if not str(getattr(response, "content", "") or "").strip():
        logger.warning("self-check rewrite returned empty — keeping the draft")
        return {}
    logger.info("self-check rewrote the answer (%d unsupported, %d missing-condition findings)",
                len(unsupported), len(missing))
    # 재작성은 초안의 '대체'다 — 초안을 지우지 않으면 다음 턴 summarize_history가
    # 지적된 초안과 수정본의 모순 쌍을 함께 요약하게 된다.
    rewrite = [AIMessage(content=str(response.content))]
    if draft_id:
        rewrite.insert(0, RemoveMessage(id=draft_id))
    return {"messages": rewrite}
