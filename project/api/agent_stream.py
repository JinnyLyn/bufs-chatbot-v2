"""Drive the agentic-RAG graph for one chat turn and yield UI events.

This is a *synchronous* generator (LangGraph's `.stream` is sync, like the
original Gradio `ChatInterface`). The chat router runs it in a worker thread and
bridges it to an async SSE response.

Event tuples yielded:
    ("token", str)   incremental answer text
    ("clear", None)  reset the streamed buffer (reserved; unused for now)
    ("done", dict)   final payload {answer, source_urls, results, intent, duration_ms,
                                    timing, sub_questions, tool_calls, model}
    ("error", str)   failure

Streaming policy: only tokens emitted by the final `aggregate_answers` node are
surfaced as the answer. Earlier nodes stay hidden behind the UI's ThinkingAnimation.

Per-stage timing is derived from the message stream's node transitions: the gap before
each chunk is attributed to the node that produced it, bucketed into
summarize_history / rewrite_query / agent (orchestrator+tools+…) / aggregate_answers.
Approximate, but enough to see where the wall time goes; the total is exact.
"""

import logging
import time

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

import config
from api.runtime import build_config, get_rag_system
from api.sources import parse_tool_results
from api.trace_context import set_trace_id
from api.translate import needs_korean_translation, to_korean

logger = logging.getLogger(__name__)

# Node whose streamed tokens are the user-facing answer.
ANSWER_NODE = "aggregate_answers"
_OUTER_NODES = {"summarize_history", "rewrite_query", "aggregate_answers"}

_FALLBACK_KO = "죄송합니다. 답변을 생성하지 못했습니다. 다시 시도해 주세요."


def _bucket(node: str) -> str:
    """Map a langgraph node name to a timing bucket."""
    if node in _OUTER_NODES:
        return node
    if not node:
        return "other"
    return "agent"  # orchestrator / tools / compress_context / fallback / collect_answer


def _extract_clarification(state) -> str | None:
    """When the graph pauses for clarification, the last AIMessage holds the prompt."""
    if not (state.next and any("clarification" in str(n) for n in state.next)):
        return None
    for msg in reversed(state.values.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return None


def _final_answer_from_state(state) -> str | None:
    """Answer adopted without an LLM call (e.g. the #177 clean-synthesis bypass, or
    aggregate's no-answers message) emits no AIMessageChunk, so the token loop collects
    nothing. On a COMPLETED run the last outer AIMessage is aggregate_answers' output —
    surface it instead of the generic failure string."""
    if state.next:
        return None
    for msg in reversed(state.values.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return str(msg.content)
    return None


def run_agent_stream(session_id: str, question: str, trace_id: str = "-"):
    # ContextVars don't cross thread boundaries — re-bind the trace id inside this
    # worker thread so any logging here carries the request's id.
    set_trace_id(trace_id)

    rs = get_rag_system()
    graph = rs.agent_graph
    config_ = build_config(session_id, trace_id=trace_id)
    t0 = time.monotonic()

    timing = {"summarize_history": 0.0, "rewrite_query": 0.0, "agent": 0.0,
              "aggregate_answers": 0.0, "other": 0.0}
    tool_call_count = 0

    try:
        # Resume a pending clarification turn, or start a fresh one.
        state = graph.get_state(config_)
        if state.next:
            graph.update_state(config_, {"messages": [HumanMessage(content=question.strip())]})
            stream_input = None
        else:
            stream_input = {"messages": [HumanMessage(content=question.strip())]}

        answer_parts: list[str] = []
        tool_contents: list[str] = []
        last_ts = time.monotonic()

        # subgraphs=True so the agent subgraph's ToolMessages are surfaced (and captured
        # here, before any in-agent context compression can drop them).
        for item in graph.stream(stream_input, config=config_, stream_mode="messages", subgraphs=True):
            now = time.monotonic()
            # subgraphs=True yields (namespace, (chunk, metadata)); otherwise (chunk, metadata).
            if len(item) == 2 and isinstance(item[0], tuple):
                _ns, (chunk, metadata) = item
            else:
                chunk, metadata = item
            node = metadata.get("langgraph_node", "")
            timing[_bucket(node)] += now - last_ts
            last_ts = now

            if isinstance(chunk, ToolMessage):
                tool_call_count += 1
                if chunk.content:
                    tool_contents.append(str(chunk.content))

            elif node == ANSWER_NODE and isinstance(chunk, AIMessageChunk) and chunk.content:
                answer_parts.append(chunk.content)
                yield ("token", chunk.content)

        # Tool calls run inside the "agent" subgraph, whose ToolMessages may not appear
        # in the outer messages stream. Read them from the final merged state too.
        final_state = graph.get_state(config_)
        for m in final_state.values.get("messages", []):
            if isinstance(m, ToolMessage) and m.content:
                tool_contents.append(str(m.content))

        answer = "".join(answer_parts).strip()

        # No streamed answer: either the graph interrupted to ask for clarification,
        # or the final answer was adopted without an LLM call and never hit the
        # token stream (#177 bypass) — read it from the final state before giving up.
        if not answer:
            answer = (_extract_clarification(final_state)
                      or _final_answer_from_state(final_state)
                      or _FALLBACK_KO)
            yield ("token", answer)

        # Language fallback: if a Korean question still receives a mostly non-Korean
        # final answer, replace only the final answer with a Korean translation.
        if needs_korean_translation(question, answer):
            logger.info("answer language mismatch — translating to Korean")
            translated = to_korean(rs.llm, answer)
            if translated and translated != answer:
                answer = translated
                yield ("clear", None)    # wipe the streamed non-Korean tokens
                yield ("token", answer)  # show the Korean answer live

        results, source_urls = parse_tool_results(tool_contents)
        sub_questions = len(final_state.values.get("rewrittenQuestions", []) or [])
        timing_ms = {k: int(v * 1000) for k, v in timing.items()}

        yield (
            "done",
            {
                "answer": answer,
                "source_urls": source_urls,
                "results": results,
                "intent": "",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "model": config.LLM_MODEL,
                "sub_questions": sub_questions,
                "tool_calls": tool_call_count,
                "timing": timing_ms,
            },
        )

    except Exception as exc:  # noqa: BLE001 — surfaced to the client as an error event
        logger.error("agent run failed: %s", exc, exc_info=True)
        yield ("error", str(exc))
