"""Tests for the hardening controls on the public API surface.

The service is published to the internet through the Cloudflare tunnel with no
authentication, so these controls are the only thing standing between an anonymous
caller and the GPU. Each test pins one of them.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("starlette", reason="starlette not installed")

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

VALID_UUID = "11111111-2222-4333-8444-555555555555"


# ---------------------------------------------------------------------------
# session_id validation
# ---------------------------------------------------------------------------

class TestSessionIdValidation:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "test-sess",           # readable label — the pre-hardening default
            "admin",               # trivially guessable, would collide across callers
            "1",
            "",
            "../../etc/passwd",    # path-ish text must never become a store key
            "11111111-2222-4333-8444-55555555555",    # one char short
            "11111111-2222-4333-8444-5555555555555",  # one char long
            "gggggggg-2222-4333-8444-555555555555",   # non-hex
        ],
    )
    def test_non_uuid_rejected(self, bad_id):
        from api.runtime import is_valid_session_id
        assert is_valid_session_id(bad_id) is False

    @pytest.mark.parametrize(
        "good_id",
        [
            VALID_UUID,
            "11111111-2222-4333-8444-555555555555".upper(),  # hex case is not significant
        ],
    )
    def test_uuid_accepted(self, good_id):
        from api.runtime import is_valid_session_id
        assert is_valid_session_id(good_id) is True

    def test_minted_session_ids_pass_validation(self):
        """create_session must not mint ids its own validator rejects."""
        from api.runtime import create_session, is_valid_session_id
        info = create_session("ko")
        assert is_valid_session_id(info["session_id"])

    def test_ensure_session_raises_on_non_uuid(self):
        from api.runtime import ensure_session
        with pytest.raises(ValueError):
            ensure_session("not-a-uuid")

    def test_stream_rejects_non_uuid_session(self):
        """The HTTP surface refuses before any agent work starts."""
        from api.chat import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/api/chat/stream",
            params={"question": "졸업학점은?", "session_id": "test-sess"},
        )
        assert resp.status_code == 422

    def test_session_read_rejects_non_uuid(self):
        from api.session import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get("/api/session/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------
# Session registry bound
# ---------------------------------------------------------------------------

class TestSessionRegistryBound:
    def test_registry_evicts_oldest_past_cap(self, monkeypatch):
        """An anonymous caller looping over fresh uuid4s must not grow memory forever."""
        import uuid

        from api import runtime
        monkeypatch.setattr(runtime, "MAX_SESSIONS", 10)
        with runtime._sessions_lock:  # noqa: SLF001 — test-only reset
            runtime._sessions.clear()

        minted = [str(uuid.uuid4()) for _ in range(25)]
        for sid in minted:
            runtime.ensure_session(sid)

        assert len(runtime._sessions) == 10  # noqa: SLF001
        # Oldest-first eviction: the newest ids survive, the earliest are gone.
        assert minted[-1] in runtime._sessions  # noqa: SLF001
        assert minted[0] not in runtime._sessions  # noqa: SLF001

    def test_eviction_also_drops_conversation_history(self, monkeypatch):
        """Capping the metadata dict is pointless if the messages keep accumulating.

        The conversation itself lives in the LangGraph checkpointer keyed by the same
        id, so eviction must reach through to delete_thread — otherwise the small
        structure is bounded and the large one still grows without limit.
        """
        import uuid
        from unittest.mock import MagicMock

        from api import runtime
        monkeypatch.setattr(runtime, "MAX_SESSIONS", 3)
        with runtime._sessions_lock:  # noqa: SLF001
            runtime._sessions.clear()

        fake_checkpointer = MagicMock()
        fake_rag = MagicMock()
        fake_rag.agent_graph.checkpointer = fake_checkpointer
        monkeypatch.setattr(runtime, "get_rag_system", lambda: fake_rag)

        minted = [str(uuid.uuid4()) for _ in range(5)]
        for sid in minted:
            runtime.ensure_session(sid)

        dropped = [c.args[0] for c in fake_checkpointer.delete_thread.call_args_list]
        assert dropped == minted[:2]

    def test_eviction_survives_a_checkpointer_that_raises(self, monkeypatch):
        """Reclaiming memory must never fail the request that triggered it."""
        import uuid
        from unittest.mock import MagicMock

        from api import runtime
        monkeypatch.setattr(runtime, "MAX_SESSIONS", 1)
        with runtime._sessions_lock:  # noqa: SLF001
            runtime._sessions.clear()

        fake_rag = MagicMock()
        fake_rag.agent_graph.checkpointer.delete_thread.side_effect = RuntimeError("boom")
        monkeypatch.setattr(runtime, "get_rag_system", lambda: fake_rag)

        runtime.ensure_session(str(uuid.uuid4()))
        runtime.ensure_session(str(uuid.uuid4()))  # must not raise
        assert len(runtime._sessions) == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:
    def _app(self):
        from api.session import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    def test_requests_over_budget_get_429(self, monkeypatch):
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_REQUESTS", 3)
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
        ratelimit.reset_for_tests()
        client = self._app()

        codes = [client.post("/api/session", json={"lang": "ko"}).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]

    def test_429_carries_retry_after(self, monkeypatch):
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_REQUESTS", 1)
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
        ratelimit.reset_for_tests()
        client = self._app()

        client.post("/api/session", json={"lang": "ko"})
        blocked = client.post("/api/session", json={"lang": "ko"})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1

    def test_budget_is_per_client_not_global(self, monkeypatch):
        """One abusive IP must not exhaust everyone else's budget."""
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_REQUESTS", 2)
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
        ratelimit.reset_for_tests()
        client = self._app()

        noisy = {"CF-Connecting-IP": "203.0.113.9"}
        for _ in range(3):
            client.post("/api/session", json={"lang": "ko"}, headers=noisy)
        blocked = client.post("/api/session", json={"lang": "ko"}, headers=noisy)
        assert blocked.status_code == 429

        other = client.post(
            "/api/session", json={"lang": "ko"}, headers={"CF-Connecting-IP": "203.0.113.10"}
        )
        assert other.status_code == 200

    def test_disabled_switch_lets_everything_through(self, monkeypatch):
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", False)
        ratelimit.reset_for_tests()
        client = self._app()

        codes = [client.post("/api/session", json={"lang": "ko"}).status_code for _ in range(30)]
        assert set(codes) == {200}

    def test_tracked_client_table_is_bounded(self, monkeypatch):
        """The limiter's own bookkeeping must not become the DoS it prevents."""
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(ratelimit, "_MAX_TRACKED_CLIENTS", 50)
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_WINDOW_S", 0.0)  # everything is stale at once
        ratelimit.reset_for_tests()
        client = self._app()

        for i in range(200):
            client.post(
                "/api/session", json={"lang": "ko"}, headers={"CF-Connecting-IP": f"198.51.100.{i % 256}"}
            )
        assert len(ratelimit._hits) <= 50  # noqa: SLF001


class TestClientKey:
    def _request(self, headers: dict, peer: str = "127.0.0.1"):
        from starlette.requests import Request
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": (peer, 1234), "method": "GET", "path": "/"})

    def test_prefers_cloudflare_client_ip(self):
        """Behind the tunnel every peer is 127.0.0.1, so the CF header is the real identity."""
        from api.ratelimit import client_key
        assert client_key(self._request({"CF-Connecting-IP": "203.0.113.7"})) == "203.0.113.7"

    def test_falls_back_to_socket_peer(self):
        from api.ratelimit import client_key
        assert client_key(self._request({}, peer="192.0.2.5")) == "192.0.2.5"


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------

class TestStreamSlot:
    def test_slots_are_released_on_exit(self):
        from api.ratelimit import StreamSlot, active_streams
        before = active_streams()
        with StreamSlot():
            assert active_streams() == before + 1
        assert active_streams() == before

    def test_release_is_idempotent(self):
        """The SSE generator's finally can run more than once; the count must not drift."""
        from api.ratelimit import StreamSlot, active_streams
        before = active_streams()
        slot = StreamSlot().acquire()
        slot.release()
        slot.release()
        assert active_streams() == before

    def test_cap_rejects_with_503(self, monkeypatch):
        from fastapi import HTTPException

        from api import ratelimit
        ratelimit.reset_for_tests(max_concurrent=1)

        held = ratelimit.StreamSlot().acquire()
        try:
            with pytest.raises(HTTPException) as excinfo:
                ratelimit.StreamSlot().acquire()
            assert excinfo.value.status_code == 503
        finally:
            held.release()

    def test_slot_freed_after_rejection_allows_next_caller(self, monkeypatch):
        from api import ratelimit
        ratelimit.reset_for_tests(max_concurrent=1)

        first = ratelimit.StreamSlot().acquire()
        first.release()
        second = ratelimit.StreamSlot().acquire()   # must not raise
        second.release()
        assert ratelimit.active_streams() == 0


# ---------------------------------------------------------------------------
# X-Test-Mode is an audit control, not a free header
# ---------------------------------------------------------------------------

class TestTestModeGating:
    def _request(self, headers: dict):
        from starlette.requests import Request
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("127.0.0.1", 1), "method": "GET", "path": "/"})

    def test_header_ignored_when_no_token_configured(self, monkeypatch):
        """Fails closed. The live deployment sets no token, so honouring a bare
        "X-Test-Mode: 1" in that case would leave the control inert where it matters."""
        import api.chat as chat
        monkeypatch.setattr(chat, "_TEST_MODE_TOKEN", "")
        assert chat._is_test_mode(self._request({"X-Test-Mode": "1"})) is False
        assert chat._is_test_mode(self._request({"X-Test-Mode": "true"})) is False

    def test_wrong_token_is_ignored(self, monkeypatch):
        """With a token configured, an attacker cannot suppress their own Q&A record."""
        import api.chat as chat
        monkeypatch.setattr(chat, "_TEST_MODE_TOKEN", "s3cret")
        assert chat._is_test_mode(self._request({"X-Test-Mode": "1"})) is False
        assert chat._is_test_mode(self._request({"X-Test-Mode": "guess"})) is False

    def test_correct_token_enables_test_mode(self, monkeypatch):
        import api.chat as chat
        monkeypatch.setattr(chat, "_TEST_MODE_TOKEN", "s3cret")
        assert chat._is_test_mode(self._request({"X-Test-Mode": "s3cret"})) is True

    def test_absent_header_is_not_test_mode(self, monkeypatch):
        import api.chat as chat
        monkeypatch.setattr(chat, "_TEST_MODE_TOKEN", "s3cret")
        assert chat._is_test_mode(self._request({})) is False


# ---------------------------------------------------------------------------
# Loopback exemption
# ---------------------------------------------------------------------------

class TestLoopbackExemption:
    def _request(self, headers: dict, peer: str):
        from starlette.requests import Request
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": (peer, 1), "method": "GET", "path": "/"})

    def test_local_eval_traffic_is_exempt(self):
        """The eval harness issues ~200 requests per run from 127.0.0.1; several runners
        do not handle 429 and would record refusals as answers."""
        from api import ratelimit
        assert ratelimit._is_loopback(self._request({}, "127.0.0.1")) is True  # noqa: SLF001

    def test_tunnel_traffic_claiming_loopback_is_not_exempt(self):
        """A remote caller must not reach the exemption. Cloudflare connects from
        127.0.0.1, so the CF header — not the peer address — is what distinguishes them."""
        from api import ratelimit
        req = self._request({"CF-Connecting-IP": "203.0.113.7"}, "127.0.0.1")
        assert ratelimit._is_loopback(req) is False  # noqa: SLF001

    def test_remote_peer_is_not_exempt(self):
        from api import ratelimit
        assert ratelimit._is_loopback(self._request({}, "203.0.113.7")) is False  # noqa: SLF001

    def test_exempt_request_is_not_rate_limited(self, monkeypatch):
        from api import ratelimit
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(ratelimit, "RATE_LIMIT_REQUESTS", 1)
        ratelimit.reset_for_tests()
        req = self._request({}, "127.0.0.1")
        for _ in range(10):
            ratelimit.check_rate_limit(req)  # must not raise


class TestClientKeyIgnoresSpoofableHeaders:
    def _request(self, headers: dict, peer: str = "127.0.0.1"):
        from starlette.requests import Request
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": (peer, 1), "method": "GET", "path": "/"})

    def test_x_forwarded_for_is_not_trusted(self):
        """Honouring it would let an attacker mint a fresh budget per request."""
        from api.ratelimit import client_key
        assert client_key(self._request({"X-Forwarded-For": "203.0.113.7"}, "198.51.100.1")) == "198.51.100.1"

    def test_true_client_ip_is_not_trusted(self):
        from api.ratelimit import client_key
        assert client_key(self._request({"True-Client-IP": "203.0.113.7"}, "198.51.100.1")) == "198.51.100.1"

    def test_cf_connecting_ip_wins_over_spoofed_headers(self):
        from api.ratelimit import client_key
        key = client_key(self._request(
            {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4"}, "127.0.0.1"))
        assert key == "203.0.113.7"


# ---------------------------------------------------------------------------
# The concurrency cap must track GPU work, not the client connection
# ---------------------------------------------------------------------------

class TestSlotTracksWorkNotConnection:
    def test_slot_is_released_by_the_producer_not_the_response(self):
        """Regression guard for the disconnect bypass.

        Releasing the slot in the SSE generator's finally returned it the instant a
        client disconnected, while the producer thread kept running the pipeline — so
        connect/disconnect in a loop ran unbounded concurrent generations with
        active_streams() reading 0. The release must live in producer()'s finally.
        """
        import inspect

        import api.chat as chat
        src = inspect.getsource(chat.chat_stream)

        producer_start = src.index("def producer():")
        consumer_start = src.index("thread = threading.Thread")
        producer_body = src[producer_start:consumer_start]

        assert "slot.release()" in producer_body, (
            "producer() must release the slot so it covers the generation, not the response"
        )
        # The consumer signals abandonment; it must not hand the slot back itself.
        consumer_body = src[consumer_start:]
        assert "abandoned.set()" in consumer_body
        assert "slot.release()" not in consumer_body.split("try:")[-1]

    def test_producer_stops_generating_when_the_client_is_gone(self):
        import inspect

        import api.chat as chat
        src = inspect.getsource(chat.chat_stream)
        assert "if abandoned.is_set():" in src, (
            "an abandoned run must stop issuing further LLM calls, not merely stop being counted"
        )


# ---------------------------------------------------------------------------
# Interactive docs are off unless asked for
# ---------------------------------------------------------------------------

class TestDocsGating:
    @staticmethod
    def _reload_server(monkeypatch):
        """Reload server.py without letting it read the real project/.env.

        server.py calls load_dotenv at import time, which would pull the deployed
        Langfuse credentials into os.environ for the rest of the session.
        """
        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        import server
        monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: False, raising=False)
        return importlib.reload(server)

    def test_docs_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENABLE_DOCS", raising=False)
        server = self._reload_server(monkeypatch)
        assert server.app.docs_url is None
        assert server.app.openapi_url is None
        assert server.app.redoc_url is None

    def test_docs_enabled_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("ENABLE_DOCS", "true")
        server = self._reload_server(monkeypatch)
        assert server.app.docs_url == "/docs"
        assert server.app.openapi_url == "/openapi.json"

    def test_root_advertises_docs_only_when_enabled(self, monkeypatch):
        monkeypatch.delenv("ENABLE_DOCS", raising=False)
        server = self._reload_server(monkeypatch)
        assert server._docs_enabled is False  # noqa: SLF001
