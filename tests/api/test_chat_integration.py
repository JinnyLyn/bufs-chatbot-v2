"""Integration tests for api/chat.py and api/agent_stream.py via FastAPI TestClient.

Design:
- Tests that use the TestClient with fully faked RAGSystem (no real LLM/Qdrant)
  are offline-capable and run in CI — but they're still marked integration because
  they exercise the full HTTP + SSE surface rather than a single function.
- Tests that need a real Ollama instance are guarded by ``skipif`` and only run
  when OLLAMA_BASE_URL is reachable.

All marked @pytest.mark.integration.

Note: TestClient + SSE (EventSourceResponse) streaming: FastAPI TestClient reads
the full SSE stream synchronously. Chunks arrive as text/event-stream lines.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
starlette = pytest.importorskip("starlette", reason="starlette not installed")

# /api/chat/stream only accepts the UUID shape the server itself mints, so tests must
# use a well-formed id rather than a readable label.
TEST_SESSION_ID = "11111111-2222-4333-8444-555555555555"
LIVE_SESSION_ID = "99999999-8888-4777-8666-555555555555"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_rag_system():
    """Return a fake RAGSystem that yields a simple done payload without LLM/Qdrant."""
    sys = MagicMock()
    done_payload = {
        "answer": "테스트 답변입니다.",
        "results": [],
        "sub_questions": 0,
        "tool_calls": 0,
        "model": "fake",
        "intent": "",
        "duration_ms": 100,
        "timing": {},
    }
    # agent_stream calls rag_system.stream() — make it yield the done event
    sys.stream.return_value = iter([("done", done_payload)])
    # Tracing must stay on the DISABLED path here: a bare MagicMock is truthy, so
    # without this the tests silently exercise the Langfuse-enabled branch.
    sys.observability.chat_turn.return_value = nullcontext(None)
    sys.observability.langchain_callbacks.return_value = None
    return sys


def _build_test_app(fake_rag=None):
    """Build a minimal FastAPI app with the chat router, injecting a fake RAG system."""
    from fastapi import FastAPI
    from api.chat import router as chat_router
    import api.runtime as runtime

    app = FastAPI()
    app.include_router(chat_router)

    # Inject the fake RAG system into the runtime so chat.py uses it
    fake = fake_rag or _make_fake_rag_system()
    runtime._rag_system = fake  # noqa: SLF001 — test-only injection
    return app, fake


# ---------------------------------------------------------------------------
# TestClient (X-Test-Mode — skips real Q&A logging)
# ---------------------------------------------------------------------------

class TestChatRouterOffline:
    def test_health_endpoint_reachable(self):
        """Smoke: the app can be constructed without raising at import time."""
        try:
            from fastapi import FastAPI
            from api.chat import router  # noqa: F401
        except ImportError as e:
            pytest.skip(f"FastAPI or chat deps missing: {e}")
        app = FastAPI()
        app.include_router(router)
        assert app is not None

    def test_stream_endpoint_returns_sse_response(self, tmp_path):
        """GET /api/chat/stream returns text/event-stream with a 'done' event."""
        try:
            from starlette.testclient import TestClient
            import api.runtime as runtime
        except ImportError as e:
            pytest.skip(f"starlette or runtime deps missing: {e}")

        app, _ = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/api/chat/stream",
            params={"question": "졸업학점은?", "session_id": TEST_SESSION_ID},
            headers={"X-Test-Mode": "1"},
        )
        # 200 or streaming: accept any 2xx — SSE may return 200
        assert resp.status_code in (200, 204), f"Unexpected status: {resp.status_code}"

    def test_stream_response_contains_done_event(self, tmp_path):
        """The streamed response must contain a 'done' event line."""
        try:
            from starlette.testclient import TestClient
            import api.runtime as runtime
        except ImportError as e:
            pytest.skip(f"starlette or runtime deps missing: {e}")

        app, _ = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/api/chat/stream",
            params={"question": "테스트", "session_id": TEST_SESSION_ID},
            headers={"X-Test-Mode": "1"},
        )
        # Both the HTTP status AND the SSE body are required — each covers a
        # different failure mode (transport vs. payload).
        assert resp.status_code == 200
        assert "done" in resp.text


# ---------------------------------------------------------------------------
# Live LLM integration — skipped when OLLAMA_BASE_URL unreachable
# ---------------------------------------------------------------------------

import os as _os
_OLLAMA_URL = _os.environ.get("OLLAMA_BASE_URL", "")


@pytest.mark.skipif(
    not _OLLAMA_URL,
    reason="OLLAMA_BASE_URL not set — skipping live LLM integration test",
)
class TestChatRouterLive:
    def test_live_stream_returns_answer(self):
        """End-to-end: real LLM answers a simple Korean question via SSE."""
        try:
            from starlette.testclient import TestClient
            from fastapi import FastAPI
            from api.chat import router
        except ImportError as e:
            pytest.skip(f"Deps missing: {e}")

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get(
            "/api/chat/stream",
            params={"question": "안녕하세요", "session_id": LIVE_SESSION_ID},
            headers={"X-Test-Mode": "1"},
        )
        assert resp.status_code == 200
        assert len(resp.text) > 0


# ---------------------------------------------------------------------------
# #177 P1 — answers adopted without an LLM call must still reach the client
# ---------------------------------------------------------------------------

class TestFinalAnswerFromState:
    def _state(self, next_=(), messages=None):
        from langchain_core.messages import AIMessage, HumanMessage
        st = MagicMock()
        st.next = tuple(next_)
        st.values = {"messages": messages if messages is not None else [
            HumanMessage(content="질문"),
            AIMessage(content="클린 답변"),
        ]}
        return st

    def test_completed_run_surfaces_last_ai_message(self):
        from api.agent_stream import _final_answer_from_state
        assert _final_answer_from_state(self._state()) == "클린 답변"

    def test_pending_run_returns_none(self):
        """state.next 비어있지 않으면 clarification 경로의 소관 — 여기선 손대지 않는다."""
        from api.agent_stream import _final_answer_from_state
        assert _final_answer_from_state(self._state(next_=("clarification",))) is None

    def test_no_ai_message_returns_none(self):
        from langchain_core.messages import HumanMessage
        from api.agent_stream import _final_answer_from_state
        st = self._state(messages=[HumanMessage(content="질문")])
        assert _final_answer_from_state(st) is None


class TestProgressStatusEvent:
    """`status` 이벤트: 답변 첫 토큰 전에 진행 단계를 알려준다 (체감 지연 개선).

    계약은 '추가만' 이다 — token/done 스트림은 그대로여야 하고, status 를 무시하는
    클라이언트도 기존과 동일하게 동작해야 한다.
    """

    def test_stage_helper_maps_nodes(self):
        from api.agent_stream import _stage_for

        assert _stage_for("aggregate_answers", 0) == "writing"
        assert _stage_for("self_check", 3) == "checking"
        assert _stage_for("orchestrator", 0) == "searching"
        # 도구 결과가 하나라도 있으면 같은 agent 노드도 '자료 확인'으로 올라간다
        assert _stage_for("orchestrator", 2) == "reading"
        # 모르는 노드도 죽지 않고 검색 단계로 떨어진다
        assert _stage_for("", 0) == "searching"

    def test_stream_emits_status_before_done(self, tmp_path):
        """status 가 첫 토큰보다 먼저 나가고, done 페이로드 키는 그대로다."""
        try:
            from starlette.testclient import TestClient
            import api.runtime as runtime  # noqa: F401
        except ImportError as e:
            pytest.skip(f"starlette or runtime deps missing: {e}")

        app, _ = _build_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/chat/stream",
            params={"question": "테스트", "session_id": TEST_SESSION_ID},
            headers={"X-Test-Mode": "1"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: status" in body, "진행 상태 이벤트가 스트림에 없다"
        assert "\"stage\"" in body
        # done 이 아니라 첫 token 보다 앞서야 의미가 있다 — done 직전으로 옮겨도
        # 통과하는 단언은 체감 개선을 전혀 보장하지 못한다.
        first_status = body.index("event: status")
        assert first_status < body.index("event: done")
        if "event: token" in body:
            assert first_status < body.index("event: token"), \
                "status 는 첫 답변 토큰보다 먼저 나가야 대기 화면이 채워진다"

    def _fake_graph(self, chunks):
        """_run_turn 이 소비하는 최소 그래프 — (chunk, metadata) 시퀀스를 그대로 흘린다."""
        from unittest.mock import MagicMock

        graph = MagicMock()
        state = MagicMock()
        state.next = None
        state.values = {"messages": []}
        graph.get_state.return_value = state
        graph.stream.return_value = iter(chunks)
        return graph

    def test_stage_machine_over_a_real_chunk_sequence(self):
        """루프 내부까지 검증: 검색 → 자료 확인 → 답변 작성 순으로 한 번씩만 나간다."""
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessageChunk, ToolMessage
        import api.agent_stream as ast_mod

        chunks = [
            (AIMessageChunk(content=""), {"langgraph_node": "orchestrator"}),
            (ToolMessage(content="Parent ID: p1\n내용", tool_call_id="t1"),
             {"langgraph_node": "tools"}),
            (AIMessageChunk(content=""), {"langgraph_node": "orchestrator"}),
            (AIMessageChunk(content="답변"), {"langgraph_node": "aggregate_answers"}),
        ]
        rs = MagicMock()
        rs.agent_graph = self._fake_graph(chunks)
        events = list(ast_mod._run_turn(rs, TEST_SESSION_ID, "질문", "tid"))

        stages = [p["stage"] for k, p in events if k == "status"]
        assert stages == ["searching", "reading", "writing"], stages
        # 검색 횟수는 실행된 도구 호출 수 — 문서 건수가 아니다.
        searches = [p["searches"] for k, p in events if k == "status"]
        assert searches == [0, 1, 1], searches
        assert any(k == "token" for k, _ in events), "토큰 스트림이 그대로 나가야 한다"

    def test_stage_never_moves_backwards(self):
        """답변 작성이 시작된 뒤 도착한 미지의 노드 청크가 단계를 되돌리면 안 된다."""
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessageChunk, ToolMessage
        import api.agent_stream as ast_mod

        chunks = [
            (AIMessageChunk(content="답변"), {"langgraph_node": "aggregate_answers"}),
            (AIMessageChunk(content=""), {"langgraph_node": "some_future_node"}),
            (ToolMessage(content="늦게 온 도구 결과", tool_call_id="t2"),
             {"langgraph_node": "tools"}),
        ]
        rs = MagicMock()
        rs.agent_graph = self._fake_graph(chunks)
        events = list(ast_mod._run_turn(rs, TEST_SESSION_ID, "질문", "tid"))

        stages = [p["stage"] for k, p in events if k == "status"]
        assert stages[-1] == "writing", stages
        assert "searching" not in stages[1:] and "reading" not in stages, stages
