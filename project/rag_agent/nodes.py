import logging
import re
from typing import List, Literal, Set
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage, ToolMessage
from langgraph.types import Command
from .graph_state import State, AgentState
from .schemas import QueryAnalysis
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


def _invoke_structured_rewrite(llm, messages):
    """Invoke with_structured_output(QueryAnalysis), trying methods until one parses.

    Ollama models support different structured-output mechanisms (qwen3.5:9b →
    function_calling; qwen3:4b-instruct → json_schema/default), so fall back across
    them rather than hard-coding one.
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
                base.with_structured_output(QueryAnalysis)
                if method is None
                else base.with_structured_output(QueryAnalysis, method=method)
            )
            return structured.invoke(messages)
        except Exception as exc:  # parse / validation error → try the next method
            last_exc = exc
    raise last_exc

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

    # Issue #15 A/B switch: when rewriting is disabled, pass the original question straight to
    # the agent as the single search query (no LLM rewrite, no clarify detour). bge-m3 already
    # handles Korean well, so the raw surface form often retrieves better than a rewrite.
    if not config.REWRITE_ENABLED:
        delete_all = [RemoveMessage(id=m.id) for m in state["messages"] if not isinstance(m, SystemMessage)]
        return {"questionIsClear": True, "messages": delete_all,
                "originalQuery": last_message.content, "rewrittenQuestions": [last_message.content]}

    context_section = (f"대화 요약:\n{conversation_summary}\n" if conversation_summary.strip() else "") + f"사용자 질문:\n{last_message.content}\n"

    response = _invoke_structured_rewrite(
        llm, [SystemMessage(content=get_rewrite_query_prompt()), HumanMessage(content=context_section)]
    )

    if response.questions and response.is_clear:
        delete_all = [RemoveMessage(id=m.id) for m in state["messages"] if not isinstance(m, SystemMessage)]
        return {"questionIsClear": True, "messages": delete_all, "originalQuery": last_message.content, "rewrittenQuestions": response.questions}

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
    if not state.get("messages"):
        human_msg = HumanMessage(content=state["question"])
        force_search = HumanMessage(content=get_force_search_instruction())
        response = llm_with_tools.invoke([sys_msg] + summary_injection + [human_msg, force_search])
        return {"messages": [human_msg, response], "tool_call_count": len(response.tool_calls or []), "iteration_count": 1}

    response = llm_with_tools.invoke([sys_msg] + summary_injection + state["messages"])
    tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []
    return {"messages": [response], "tool_call_count": len(tool_calls) if tool_calls else 0, "iteration_count": 1}

def _collect_unique_tool_contents(state: AgentState) -> List[str]:
    """Unique ToolMessage contents in first-seen order (the agent's actual evidence)."""
    seen = set()
    unique_contents = []
    for m in state["messages"]:
        if isinstance(m, ToolMessage) and m.content not in seen:
            unique_contents.append(m.content)
            seen.add(m.content)
    return unique_contents


# Matches the "Parent ID: <id>" line the retrieval tools prepend to every chunk block
# (see ToolFactory._search_child_chunks / _retrieve_parent_chunks output format).
_PARENT_ID_LINE = re.compile(r"(?m)^Parent ID:\s*(.+?)\s*$")


def _expand_parent_context(unique_contents: List[str]) -> List[str]:
    """Auto parent expansion (issue #126): load the parent originals for the child chunks
    the agent actually saw.

    Returns formatted parent blocks, deduped by parent_id in first-seen order (매칭:
    #126 시뮬레이션의 "본 순서 dedup"), capped at PARENT_EXPANSION_MAX_PARENTS /
    PARENT_EXPANSION_MAX_CHARS. Any failure degrades to fewer (or zero) blocks — the
    answer path must never die on an expansion error, but every failure is logged
    with the offending parent_id so it stays traceable.
    """
    ordered_ids: List[str] = []
    seen_ids = set()
    for content in unique_contents:
        for pid in _PARENT_ID_LINE.findall(content or ""):
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                ordered_ids.append(pid)

    if not ordered_ids:
        return []

    try:
        store = _get_parent_store()
    except Exception:
        logger.exception("parent expansion disabled for this call: parent store init failed")
        return []

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

    if config.PARENT_EXPANSION_ENABLED and unique_contents:
        parent_blocks = _expand_parent_context(unique_contents)
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
    return {"messages": [response]}

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

    return {"context_summary": new_summary, "messages": [RemoveMessage(id=m.id) for m in messages[1:]]}

def collect_answer(state: AgentState):
    last_message = state["messages"][-1]
    is_valid = isinstance(last_message, AIMessage) and last_message.content and not last_message.tool_calls
    answer = last_message.content if is_valid else "답변을 생성하지 못했습니다."
    return {
        "final_answer": answer,
        "agent_answers": [{"index": state["question_index"], "question": state["question"], "answer": answer}]
    }
# --- End of Agent Nodes---

def aggregate_answers(state: State, llm):
    if not state.get("agent_answers"):
        return {"messages": [AIMessage(content="생성된 답변이 없습니다.")]}

    sorted_answers = sorted(state["agent_answers"], key=lambda x: x["index"])

    formatted_answers = ""
    for i, ans in enumerate(sorted_answers, start=1):
        formatted_answers += (f"\n답변 {i}:\n"f"{ans['answer']}\n")

    user_message = HumanMessage(content=f"""원래 사용자 질문: {state["originalQuery"]}\n검색된 답변:{formatted_answers}""")
    synthesis_response = llm.invoke([SystemMessage(content=get_aggregation_prompt()), user_message])
    return {"messages": [AIMessage(content=synthesis_response.content)]}
