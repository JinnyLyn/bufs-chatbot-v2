"""SSE chat endpoint matching the CamChat frontend's EventSource contract.

The agentic graph runs in a worker thread (its `.stream` is blocking); events are
handed to the async response via a thread-safe queue so the event loop never blocks.

Each request gets a trace_id (logged on every line via TraceFilter), structured
[chat-IN]/[chat-OUT]/PIPELINE_TIMING logs, and a Q&A JSONL record.
"""

import asyncio
import json
import logging
import os
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

import config
from api.agent_stream import run_agent_stream
from api.qa_logger import get_qa_logger, set_skip_log
from api.ratelimit import BUSY_RETRY_AFTER_S, StreamSlot, check_rate_limit, reject_if_saturated
from api.runtime import ensure_session
from api.trace_context import new_trace_id, set_trace_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


def _finalize(tid: str, session_id: str, question: str, payload: dict, t0: float) -> None:
    """Log [chat-OUT] + PIPELINE_TIMING and persist the Q&A record."""
    timing = payload.get("timing", {})
    sources = sorted({(r.get("source") or "") for r in payload.get("results", []) if r.get("source")})
    total_ms = payload.get("duration_ms", int((time.monotonic() - t0) * 1000))

    logger.info(
        "[chat-OUT] tid=%s sid=%s answer_chars=%d results=%d sources=%d total_ms=%d",
        tid, session_id[:8], len(payload.get("answer", "")), len(payload.get("results", [])),
        len(sources), total_ms,
    )
    logger.info(
        "PIPELINE_TIMING tid=%s total=%dms summarize=%dms rewrite=%dms agent=%dms "
        "aggregate=%dms other=%dms sub_q=%d tool_calls=%d model=%s%s",
        tid, total_ms, timing.get("summarize_history", 0), timing.get("rewrite_query", 0),
        timing.get("agent", 0), timing.get("aggregate_answers", 0), timing.get("other", 0),
        payload.get("sub_questions", 0), payload.get("tool_calls", 0), payload.get("model", ""),
        # #176: appended only when the lever is on so the OFF log line is unchanged.
        f" self_check={timing['self_check']}ms" if "self_check" in timing else "",
    )
    try:
        get_qa_logger().log(
            question=question, answer=payload.get("answer", ""), session_id=session_id,
            trace_id=tid, model=payload.get("model", ""), intent=payload.get("intent", ""),
            duration_ms=total_ms, num_results=len(payload.get("results", [])), sources=sources,
            sub_questions=payload.get("sub_questions", 0), tool_calls=payload.get("tool_calls", 0),
            timing=timing,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Q&A log failed: %s", exc)


# Suppressing a request's Q&A record is an audit-trail control, so it must not be
# something any caller can flip by adding a header. X-Test-Mode is honoured ONLY when
# TEST_MODE_TOKEN is configured and the header carries that exact value.
#
# Fails CLOSED: with no token configured the header is ignored entirely. Honouring a
# bare "X-Test-Mode: 1" whenever the token happened to be unset would have made this
# opt-in-to-be-secure — the live deployment does not set the variable, so the control
# would have been inert exactly where it matters. Eval and regression runs that need
# to stay out of the production Q&A log should set CHAT_LOG_DISABLED server-side
# (config.py), which is not reachable from a request at all.
_TEST_MODE_TOKEN = os.environ.get("TEST_MODE_TOKEN", "").strip()


def _is_test_mode(request: Request) -> bool:
    header = request.headers.get("X-Test-Mode", "").strip()
    if not header or not _TEST_MODE_TOKEN:
        return False
    # Compare as bytes: secrets.compare_digest raises TypeError on str containing
    # non-ASCII, and this header is attacker-controlled — "X-Test-Mode: 한글" would
    # otherwise be an unhandled 500 on a public endpoint. Encoding sidesteps the
    # restriction while keeping the comparison constant-time.
    try:
        return secrets.compare_digest(header.encode("utf-8"), _TEST_MODE_TOKEN.encode("utf-8"))
    except Exception:  # noqa: BLE001 — never let audit gating raise into the request
        return False


@router.get("/stream")
async def chat_stream(
    request: Request,
    session_id: str = Query(..., description="세션 ID (= LangGraph thread_id)"),
    question: str = Query(..., min_length=1, max_length=2000, description="질문"),
):
    """GET /api/chat/stream?session_id=&question= → SSE.

    Emits `token` (incremental), `status` (coarse progress, purely informational),
    `done` (final payload) and `error` events, exactly as the frontend `useChat` hook
    expects. `X-Test-Mode` header skips Q&A logging.
    """
    # Order matters: reject cheaply before anything expensive. Rate limit first (a
    # flood costs nothing to refuse), then validate the id, then claim a GPU slot.
    check_rate_limit(request)
    try:
        ensure_session(session_id)
    except ValueError:
        # Reject before any LLM work: an unrecognised id shape is never one we minted.
        raise HTTPException(status_code=422, detail="session_id must be a UUID.") from None
    # Advisory only — see reject_if_saturated. The slot is actually taken inside the
    # generator, because that is the only place a matching release is guaranteed.
    reject_if_saturated()
    tid = new_trace_id()
    set_trace_id(tid)
    is_test = _is_test_mode(request)
    t0 = time.monotonic()
    logger.info(
        "[chat-IN] tid=%s sid=%s q_chars=%d q=%r model=%s test=%s",
        tid, session_id[:8], len(question), question[:80], config.LLM_MODEL, is_test,
    )

    async def event_generator():
        # Re-bind ContextVars for this generator's execution context.
        set_trace_id(tid)
        set_skip_log(is_test)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # Set when the client is gone. The producer checks it between graph events, so an
        # abandoned run stops issuing further LLM calls instead of running to completion
        # for nobody. It cannot interrupt the call already in flight — graph.stream() is
        # blocking with no cancellation token — so it takes effect at the next event.
        abandoned = threading.Event()

        # Acquired HERE, not in the endpoint. A slot taken before the generator runs is
        # stranded whenever the generator never runs at all — a client that disconnects
        # before the response starts, or any exception between the acquire and this
        # point. Nothing releases those, so the cap would wedge the service at 503
        # permanently. Inside the generator every acquire is paired with a release,
        # either by the producer thread or by the thread-start failure path below.
        try:
            slot = StreamSlot().acquire()
        except HTTPException:
            # The response has already begun, so a status code is no longer available —
            # report saturation as a stream error instead.
            logger.info("[chat-BUSY] tid=%s all stream slots in use", tid)
            # `code`/`retry_after` mirror the 503 + Retry-After the endpoint would have
            # sent before the stream began, so the frontend can lock its retry button for
            # the same interval (reports/CamChat-장애대응.pdf §7.4).
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": "지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요.",
                        "code": "busy",
                        "retry_after": BUSY_RETRY_AFTER_S,
                    },
                    ensure_ascii=False,
                ),
            }
            return

        def _post(item) -> None:
            """Hand an item to the event loop, tolerating a loop that has already closed."""
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # loop shut down under us; the consumer is gone anyway

        def producer():
            try:
                for event in run_agent_stream(session_id, question, trace_id=tid):
                    if abandoned.is_set():
                        logger.info("[chat-ABORT] tid=%s client gone — stopping generation", tid)
                        break
                    _post(event)
            except Exception as exc:  # noqa: BLE001
                _post(("error", str(exc)))
            finally:
                # The slot is released HERE, by the thread that does the GPU work, and
                # NOT in the consumer's finally. Releasing it when the response ends
                # would return the slot the instant a client disconnects while this
                # thread kept running the pipeline — so connect/disconnect in a loop
                # would run unbounded concurrent generations with the cap reading zero.
                # Slot lifetime has to track the work, not the connection.
                slot.release()
                _post(None)  # sentinel

        thread = threading.Thread(target=producer, daemon=True)
        try:
            thread.start()
        except BaseException:
            # Nothing else will ever release it if the thread never ran.
            slot.release()
            raise

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, payload = item

                if kind == "token":
                    yield {"event": "token", "data": json.dumps({"token": payload}, ensure_ascii=False)}
                elif kind == "clear":
                    yield {"event": "clear", "data": "{}"}
                elif kind == "status":
                    # Progress only. A client that ignores this event still gets the
                    # exact same token/done stream it got before.
                    yield {"event": "status", "data": json.dumps(payload, ensure_ascii=False)}
                elif kind == "done":
                    _finalize(tid, session_id, question, payload, t0)
                    yield {"event": "done", "data": json.dumps(payload, ensure_ascii=False)}
                elif kind == "error":
                    logger.error("[chat-ERR] tid=%s %s", tid, payload)
                    yield {
                        "event": "error",
                        "data": json.dumps(
                            {"message": "처리 중 오류가 발생했습니다. 다시 시도해 주세요."},
                            ensure_ascii=False,
                        ),
                    }
        finally:
            # On disconnect this generator is closed and GeneratorExit lands on the await
            # above. Signal the producer; it releases the slot when it actually stops.
            abandoned.set()

    return EventSourceResponse(event_generator())
