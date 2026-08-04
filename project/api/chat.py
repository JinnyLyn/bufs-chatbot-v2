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
from api.auth_token import resolve_user_id
from api.qa_logger import get_qa_logger, set_skip_log, should_skip_log
from api.runtime import ensure_session
from api.trace_context import new_trace_id, set_trace_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


def _save_user_history(
    user_id: Optional[int], session_id: str, question: str, payload: dict
) -> None:
    """로그인 사용자의 한 턴을 chat_messages에 저장 (비로그인이면 no-op).

    저장에 실패해도 채팅 응답 자체에는 영향을 주지 않는다 — 경고만 남기고 넘어간다.
    X-Test-Mode(should_skip_log)면 저장하지 않는다: 평가·회귀 러너가 실사용자 이력을
    오염시키지 않도록.
    """
    if user_id is None or should_skip_log():
        return
    try:
        from db.user_db import insert_chat_message

        insert_chat_message(
            user_id=user_id,
            session_id=session_id,
            question=question,
            answer=payload.get("answer", ""),
            intent=payload.get("intent", "") or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_messages 저장 실패 (user_id=%s): %s", user_id, exc)


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
    access_token: Optional[str] = Query(
        None,
        description="로그인 토큰 (선택). EventSource는 헤더를 못 실어서 쿼리로 받는다.",
    ),
):
    """GET /api/chat/stream?session_id=&question= → SSE.

    Emits `token` (incremental), `done` (final payload) and `error` events, exactly
    as the frontend `useChat` hook expects. `X-Test-Mode` header skips Q&A logging.

    `access_token`이 유효하면 답변 완료 시 해당 계정의 질문 이력으로도 저장된다.
    토큰이 없거나 만료됐어도 채팅은 그대로 동작한다 — 로그인은 선택이다.
    """
    ensure_session(session_id)
    tid = new_trace_id()
    set_trace_id(tid)
    is_test = request.headers.get("X-Test-Mode", "").strip().lower() in {"1", "true", "yes", "on"}
    user_id = resolve_user_id(access_token)
    t0 = time.monotonic()
    logger.info(
        "[chat-IN] tid=%s sid=%s q_chars=%d q=%r model=%s test=%s user=%s",
        tid, session_id[:8], len(question), question[:80], config.LLM_MODEL, is_test,
        user_id if user_id is not None else "-",
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
                _save_user_history(user_id, session_id, question, payload)
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
