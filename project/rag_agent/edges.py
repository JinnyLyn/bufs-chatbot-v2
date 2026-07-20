from typing import Literal
from langchain_core.messages import ToolMessage
from langgraph.types import Send
from .graph_state import State, AgentState
import config
from config import MAX_ITERATIONS, MAX_TOOL_CALLS

# Tool outputs that carry no usable evidence: the exact no-result markers and the
# stable error prefixes returned by ToolFactory (see rag_agent/tools.py). Kept as
# prefix/equality checks mirroring how api/sources.py consumes the same strings.
_NO_EVIDENCE_MARKERS = frozenset({"NO_RELEVANT_CHUNKS", "NO_PARENT_DOCUMENT", "NO_PARENT_DOCUMENTS"})
_ERROR_PREFIXES = ("RETRIEVAL_ERROR", "PARENT_RETRIEVAL_ERROR")


def _has_tool_evidence(state: AgentState) -> bool:
    """True when the agent collected at least one informative tool result — either a
    ToolMessage that is not a no-result/error marker, or a non-empty compressed
    context summary (compression removes the ToolMessages it summarized)."""
    if state.get("context_summary", "").strip():
        return True
    for m in state.get("messages", []):
        if not isinstance(m, ToolMessage):
            continue
        content = m.content if isinstance(m.content, str) else str(m.content or "")
        content = content.strip()
        if content and content not in _NO_EVIDENCE_MARKERS and not content.startswith(_ERROR_PREFIXES):
            return True
    return False


def route_after_rewrite(state: State) -> Literal["request_clarification", "agent"]:
    if not state.get("questionIsClear", False):
        return "request_clarification"
    else:
        return [
                Send("agent", {"question": query, "question_index": idx, "messages": []})
                for idx, query in enumerate(state["rewrittenQuestions"])
            ]

def route_after_orchestrator_call(state: AgentState) -> Literal["tools", "fallback_response", "collect_answer", "clean_synthesis"]:
    iteration = state.get("iteration_count", 0)
    tool_count = state.get("tool_call_count", 0)

    if iteration >= MAX_ITERATIONS or tool_count > MAX_TOOL_CALLS:
        return "fallback_response"

    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", None) or []

    if not tool_calls:
        # Issue #126: the agent loop's own answer-synthesis step loses ~1/3 of the
        # generation failures that a clean single-shot call over the same evidence
        # recovers. When enabled and evidence exists, hand the final answer to the
        # clean_synthesis node instead of keeping the orchestrator's draft. Without
        # evidence (out-of-scope refusals, failed retrieval) the orchestrator's own
        # answer — typically the refusal sentence — is kept unchanged.
        if config.CLEAN_SYNTHESIS_ENABLED and _has_tool_evidence(state):
            return "clean_synthesis"
        return "collect_answer"

    return "tools"
