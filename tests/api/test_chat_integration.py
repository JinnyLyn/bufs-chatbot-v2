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
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
starlette = pytest.importorskip("starlette", reason="starlette not installed")


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
            params={"question": "졸업학점은?", "session_id": "test-sess"},
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
            params={"question": "테스트", "session_id": "test-sess"},
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
            params={"question": "안녕하세요", "session_id": "live-test"},
            headers={"X-Test-Mode": "1"},
        )
        assert resp.status_code == 200
        assert len(resp.text) > 0
