import re
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

# Refusal-class draft detection for CLEAN_SYNTHESIS_MODE=refusal_only. Same family as the
# #126 forensic classification regex, anchored on the prompts' canonical refusal phrasings
# ("제공된 자료에는 해당 정보가 없습니다", "…정보를 찾지 못했습니다"). Known limitation
# (accepted, measured): a correct answer that merely CONTAINS a refusal-style caveat can
# match — the cost is one extra clean-synthesis pass over evidence the draft already used
# (worst observed in the PR #144 A/B: 1.0 → 0.7, usually preserved), never a lost answer.
_REFUSAL_PATTERN = re.compile(r"자료에.{0,10}없|확인할 수 없|정보가 없|찾지 못")


def _is_refusal_draft(message) -> bool:
    content = getattr(message, "content", "")
    if not isinstance(content, str):
        content = str(content or "")
    return bool(_REFUSAL_PATTERN.search(content))


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
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", None) or []

    if not tool_calls:
        # Issue #126: the agent loop's own answer-synthesis step loses ~1/3 of the
        # generation failures that a clean single-shot call over the same evidence
        # recovers. When enabled and evidence exists, hand the final answer to the
        # clean_synthesis node instead of keeping the orchestrator's draft. Without
        # evidence (out-of-scope refusals, failed retrieval) the orchestrator's own
        # answer — typically the refusal sentence — is kept unchanged.
        # In refusal_only mode (default; PR #144 A/B: "always" is accuracy-neutral
        # because re-synthesis degrades already-correct drafts) the reroute fires
        # only when the draft itself is a refusal — evidence exists yet the agent
        # claims there is none (#126's "근거-맹시 거부" bucket).
        if config.CLEAN_SYNTHESIS_ENABLED and _has_tool_evidence(state):
            if config.CLEAN_SYNTHESIS_MODE == "always" or _is_refusal_draft(last_message):
                return "clean_synthesis"
        return "collect_answer"

    if iteration >= MAX_ITERATIONS or tool_count > MAX_TOOL_CALLS:
        return "fallback_response"

    return "tools"
