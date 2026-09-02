"""Drive the agentic-RAG graph for one chat turn and yield UI events.

This is a *synchronous* generator (LangGraph's `.stream` is sync, like the
original Gradio `ChatInterface`). The chat router runs it in a worker thread and
bridges it to an async SSE response.

Event tuples yielded:
    ("token", str)   incremental answer text
    ("clear", None)  reset the streamed buffer (reserved; unused for now)
    ("status", dict) coarse progress {stage, searches} — see the _STAGE_* constants
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
from api.sources import parse_tool_results, strip_source_footer
from api.trace_context import set_trace_id
from api.translate import needs_korean_translation, to_korean

logger = logging.getLogger(__name__)

# Node whose streamed tokens are the user-facing answer.
ANSWER_NODE = "aggregate_answers"
# #176: the self_check REWRITE (when the lever is on and the judge fails the draft)
# REPLACES the aggregated answer — its tokens re-stream after a "clear". Judge-call
# tokens are internal and carry this tag so they are never surfaced.
SELF_CHECK_NODE = "self_check"
_SELF_CHECK_JUDGE_TAG = "selfcheck_judge"
_OUTER_NODES = {"summarize_history", "rewrite_query", "aggregate_answers", "self_check"}

_FALLBACK_KO = "죄송합니다. 답변을 생성하지 못했습니다. 다시 시도해 주세요."

# Coarse progress stages surfaced to the UI. The first answer token lands 4~17s into a
# turn (2026-09-01 실측), and until then the user sees an animation with no information —
# that dead air is what "느리다" reports are actually about. Stage KEYS are shipped, not
# labels: the frontend owns the wording and its ko/en translation.
_STAGE_SEARCHING = "searching"      # orchestrator deciding / running a retrieval tool
_STAGE_READING = "reading"          # at least one tool result came back
_STAGE_WRITING = "writing"          # final answer generation started
_STAGE_CHECKING = "checking"        # self-check rewrite pass (#176)

# node → stage for nodes that map cleanly. Anything else inside the agent subgraph is
# "searching"; ``_stage_for`` keeps that fallback so a new node never breaks the stream.
# Every node that WRITES the user-facing answer is listed, not just the streaming one:
# clean_synthesis / fallback_response also produce the answer, and leaving them unmapped
# labelled their whole generation window "찾은 자료를 확인하고 있어요".
_NODE_STAGE = {
    "summarize_history": _STAGE_SEARCHING,
    "rewrite_query": _STAGE_SEARCHING,
    "aggregate_answers": _STAGE_WRITING,
    "clean_synthesis": _STAGE_WRITING,
    "fallback_response": _STAGE_WRITING,
    "self_check": _STAGE_CHECKING,
}

# Progress only ever moves forward. Without this a chunk from an unmapped node arriving
# after answer generation started would drive the label back to "검색 중", which reads as
# the system having lost its place. Not reachable in today's graph — kept so it stays
# unreachable when nodes are added.
_STAGE_RANK = {
    _STAGE_SEARCHING: 0,
    _STAGE_READING: 1,
    _STAGE_WRITING: 2,
    _STAGE_CHECKING: 3,
}


def _stage_for(node: str, searches: int) -> str:
    """Stage key for a langgraph node — retrieval evidence promotes searching→reading."""
    stage = _NODE_STAGE.get(node)
    if stage is not None:
        return stage
    return _STAGE_READING if searches else _STAGE_SEARCHING


def _advanced(current: str, candidate: str) -> bool:
    """True when ``candidate`` is a later stage than ``current`` (never backwards)."""
    return _STAGE_RANK.get(candidate, -1) > _STAGE_RANK.get(current, -1)


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
    nothing. On a COMPLETED run the last outer AIMessage is the graph's final answer —
    the source of truth when the token stream and state can disagree (#176 rewrite
    paths, #177 bypass)."""
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
    saw_terminal = False
    with rs.observability.chat_turn(session_id, question, trace_id) as root:
        try:
            for event in _run_turn(rs, session_id, question, trace_id):
                kind, payload = event
                if root is not None:
                    if kind == "done":
                        root.update(output={"answer": payload["answer"]})
                    elif kind == "error":
                        root.update(level="ERROR", status_message=str(payload))
                if kind in ("done", "error"):
                    saw_terminal = True
                yield event
        finally:
            # A client that disconnects mid-turn closes this generator
            # (GeneratorExit), which OTel does not error-mark — without this the
            # abandoned turn exports as a clean-looking trace with no output.
            if root is not None and not saw_terminal:
                root.update(level="WARNING", status_message="client disconnected")


def _run_turn(rs, session_id: str, question: str, trace_id: str):
    graph = rs.agent_graph
    config_ = build_config(session_id)
    t0 = time.monotonic()

    timing = {"summarize_history": 0.0, "rewrite_query": 0.0, "agent": 0.0,
              "aggregate_answers": 0.0, "other": 0.0}
    if config.SELF_CHECK_ENABLED:
        # Keyed only when the node exists so the OFF payload stays byte-identical.
        timing["self_check"] = 0.0
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
        rewrite_started = False
        last_ts = time.monotonic()

        # First status goes out before the graph produces anything, so the UI has
        # something to show within milliseconds instead of after the first token.
        stage = _STAGE_SEARCHING
        yield ("status", {"stage": stage, "searches": 0})

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

            # Emit only on forward movement: one event per stage, not per chunk.
            next_stage = _stage_for(node, tool_call_count)
            if _advanced(stage, next_stage):
                stage = next_stage
                yield ("status", {"stage": stage, "searches": tool_call_count})

            if isinstance(chunk, ToolMessage):
                # #89: a budget-refused call is not an executed search — counting it would
                # distort the tool_calls metric exactly in the runs the lever fires on.
                if str(chunk.content or "").startswith("SEARCH_BUDGET_EXCEEDED"):
                    continue
                tool_call_count += 1
                if chunk.content:
                    tool_contents.append(str(chunk.content))
                # Evidence just landed — say so now rather than on the next chunk. The
                # count is of executed SEARCHES (the same quantity as the tool_calls
                # metric), not of retrieved documents: one search returns many chunks, so
                # "자료 N건" would contradict the source list shown after the answer.
                if _advanced(stage, _STAGE_READING):
                    stage = _STAGE_READING
                yield ("status", {"stage": stage, "searches": tool_call_count})

            elif node == ANSWER_NODE and isinstance(chunk, AIMessageChunk) and chunk.content:
                answer_parts.append(chunk.content)
                yield ("token", chunk.content)

            elif (node == SELF_CHECK_NODE and isinstance(chunk, AIMessageChunk)
                  and chunk.content and _SELF_CHECK_JUDGE_TAG not in (metadata.get("tags") or [])):
                # Rewrite tokens: wipe the streamed draft once, then stream the replacement.
                if not rewrite_started:
                    rewrite_started = True
                    answer_parts = []
                    yield ("clear", None)
                answer_parts.append(chunk.content)
                yield ("token", chunk.content)

        # Tool calls run inside the "agent" subgraph, whose ToolMessages may not appear
        # in the outer messages stream. Read them from the final merged state too.
        final_state = graph.get_state(config_)
        for m in final_state.values.get("messages", []):
            if isinstance(m, ToolMessage) and m.content:
                tool_contents.append(str(m.content))

        answer = "".join(answer_parts).strip()

        # #176: a self-check rewrite replaced the draft mid-stream. If the rewrite call
        # died after partial tokens or produced only whitespace, the node kept the draft
        # in STATE — reconcile so the shipped answer always matches the graph's answer.
        if rewrite_started:
            state_answer = (_final_answer_from_state(final_state) or "").strip()
            if state_answer and state_answer != answer:
                answer = state_answer
                yield ("clear", None)
                yield ("token", answer)

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
            translated = to_korean(rs.llm, answer, callbacks=rs.observability.langchain_callbacks())
            if translated and translated != answer:
                answer = translated
                yield ("clear", None)    # wipe the streamed non-Korean tokens
                yield ("token", answer)  # show the Korean answer live

        # Applied last so every path (stream, state reconcile, translation) is covered;
        # the frontend replaces the streamed text with this payload's answer on "done".
        answer = strip_source_footer(answer)

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
