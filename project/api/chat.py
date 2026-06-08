"""SSE chat endpoint matching the CamChat frontend's EventSource contract.

The agentic graph runs in a worker thread (its `.stream` is blocking); events are
handed to the async response via a thread-safe queue so the event loop never blocks.

Each request gets a trace_id (logged on every line via TraceFilter), structured
[chat-IN]/[chat-OUT]/PIPELINE_TIMING logs, and a Q&A JSONL record.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

import config
from api.agent_stream import run_agent_stream
from api.qa_logger import get_qa_logger, set_skip_log
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
        "aggregate=%dms other=%dms sub_q=%d tool_calls=%d model=%s",
        tid, total_ms, timing.get("summarize_history", 0), timing.get("rewrite_query", 0),
        timing.get("agent", 0), timing.get("aggregate_answers", 0), timing.get("other", 0),
        payload.get("sub_questions", 0), payload.get("tool_calls", 0), payload.get("model", ""),
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


@router.get("/stream")
async def chat_stream(
    request: Request,
    session_id: str = Query(..., description="세션 ID (= LangGraph thread_id)"),
    question: str = Query(..., min_length=1, max_length=2000, description="질문"),
    access_token: Optional[str] = Query(None, description="미사용 (로그인 기능 제외)"),
):
    """GET /api/chat/stream?session_id=&question= → SSE.

    Emits `token` (incremental), `done` (final payload) and `error` events, exactly
    as the frontend `useChat` hook expects. `X-Test-Mode` header skips Q&A logging.
    """
    ensure_session(session_id)
    tid = new_trace_id()
    set_trace_id(tid)
    is_test = request.headers.get("X-Test-Mode", "").strip().lower() in {"1", "true", "yes", "on"}
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

        def producer():
            try:
                for event in run_agent_stream(session_id, question, trace_id=tid):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            kind, payload = item

            if kind == "token":
                yield {"event": "token", "data": json.dumps({"token": payload}, ensure_ascii=False)}
            elif kind == "clear":
                yield {"event": "clear", "data": "{}"}
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

    return EventSourceResponse(event_generator())
