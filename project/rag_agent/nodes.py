import logging
from typing import List, Literal, Set
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage, ToolMessage
from langgraph.types import Command
from .graph_state import State, AgentState
from .schemas import QueryAnalysis, UserSlots
from .prompts import *
import config
from utils import estimate_context_tokens
from config import BASE_TOKEN_THRESHOLD, TOKEN_GROWTH_FACTOR

logger = logging.getLogger(__name__)


def _invoke_structured(llm, messages, schema):
    """Invoke with_structured_output(schema), trying methods until one parses.

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
                base.with_structured_output(schema)
                if method is None
                else base.with_structured_output(schema, method=method)
            )
            return structured.invoke(messages)
        except Exception as exc:  # parse / validation error → try the next method
            last_exc = exc
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
    # Issue #145 처방 1: surface the user's stated conditions on EVERY orchestrator turn
    # (same pattern as summary_injection — injected, not appended to state, so the message
    # history stays clean). Empty block → no injection → byte-identical to OFF.
    slot_text = format_user_slots(state.get("user_slots") or {}) if config.SLOT_EXTRACTION_ENABLED else ""
    # v2 wording (S1 A/B: v1's "판단 근거 명시/없으면 그 사실을 명시" meta-instructions
    # acted as refusal bait — refusals 33→50. Keep only the positive apply-instruction.)
    slot_injection = (
        [HumanMessage(content=(
            f"{slot_text}\n\n"
            "위 조건은 사용자가 질문에 명시한 개인 상황입니다. 규정이 조건(학번·신분·유형 등)에 따라 "
            "달라지면 위 조건에 해당하는 경우를 기준으로 답하세요."
        ))]
        if slot_text else []
    )
    if not state.get("messages"):
        human_msg = HumanMessage(content=state["question"])
        force_search = HumanMessage(content="이 질문에 답하려면 첫 단계로 반드시 'search_child_chunks'를 호출하세요.")
        response = llm_with_tools.invoke([sys_msg] + summary_injection + slot_injection + [human_msg, force_search])
        return {"messages": [human_msg, response], "tool_call_count": len(response.tool_calls or []), "iteration_count": 1}

    response = llm_with_tools.invoke([sys_msg] + summary_injection + slot_injection + state["messages"])
    tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []
    return {"messages": [response], "tool_call_count": len(tool_calls) if tool_calls else 0, "iteration_count": 1}

def fallback_response(state: AgentState, llm):
    seen = set()
    unique_contents = []
    for m in state["messages"]:
        if isinstance(m, ToolMessage) and m.content not in seen:
            unique_contents.append(m.content)
            seen.add(m.content)

    context_summary = state.get("context_summary", "").strip()

    context_parts = []
    if context_summary:
        context_parts.append(f"## 압축된 조사 컨텍스트 (이전 반복)\n\n{context_summary}")
    if unique_contents:
        context_parts.append(
            "## 검색된 데이터 (현재 반복)\n\n" +
            "\n\n".join(f"--- 데이터 출처 {i} ---\n{content}" for i, content in enumerate(unique_contents, 1))
        )

    context_text = "\n\n".join(context_parts) if context_parts else "문서에서 검색된 데이터가 없습니다."

    prompt_content = (
        f"사용자 질문: {state.get('question')}\n\n"
        f"{context_text}\n\n"
        f"지시:\n위 데이터만 사용해 가능한 한 최선의 답변을 작성하세요."
    )
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

    content = f"""원래 사용자 질문: {state["originalQuery"]}\n검색된 답변:{formatted_answers}"""

    # Issue #145 처방 1: final condition-coverage check. Only appended when slots exist,
    # so slot-free questions aggregate on byte-identical input.
    slot_text = format_user_slots(state.get("userSlots") or {}) if config.SLOT_EXTRACTION_ENABLED else ""
    if slot_text:
        # v2 wording: the v1 "없으면 그 사실을 명시하세요" clause invited refusal-style
        # hedging (S1 A/B refusals +17) — keep only the condition-application instruction.
        content += (
            f"\n\n{slot_text}\n\n"
            "조건에 따라 규정이 달라지는 부분은 사용자의 조건 기준으로 답하세요."
        )

    user_message = HumanMessage(content=content)
    synthesis_response = llm.invoke([SystemMessage(content=get_aggregation_prompt()), user_message])
    return {"messages": [AIMessage(content=synthesis_response.content)]}
