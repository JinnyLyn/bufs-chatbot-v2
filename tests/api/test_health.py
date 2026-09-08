"""GET /api/health — the external-monitor probe (reports/CamChat-장애대응.pdf §5).

It must be reachable through the tunnel's ``/api/`` ingress and must not leak the
runtime details that ``/health`` exposes (model name, Ollama URL, paths, GPU).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("starlette", reason="starlette not installed")

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _client() -> TestClient:
    from api.health import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_public_health_is_ok_and_minimal():
    """Exact equality is the leak guard: any extra key (model, paths, GPU) fails this."""
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_public_health_accepts_head():
    """UptimeRobot (free plan) probes with HEAD; a 405 would read as an outage."""
    resp = _client().head("/api/health")
    assert resp.status_code == 200
    assert resp.content == b""


def test_health_llm_error_keeps_exception_detail_out_of_response(monkeypatch, caplog):
    """CodeQL py/stack-trace-exposure: the probe must not echo the exception text (requests
    embeds URL, proxy and socket detail). Detail belongs in the log; the body keeps the
    keys scripts/healthcheck.* read (``status``, ``ollama_base_url``) plus the class name."""
    import logging

    requests = pytest.importorskip("requests")
    import api.health as health

    def boom(*_args, **_kwargs):
        raise requests.ConnectionError("HTTPConnectionPool(host='internal-gpu-box') secret-detail")

    monkeypatch.setattr(health.requests, "get", boom)
    with caplog.at_level(logging.WARNING, logger="api.health"):
        resp = _client().get("/health/llm")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "ConnectionError"
    assert "ollama_base_url" in body
    assert "secret-detail" not in resp.text
    assert "internal-gpu-box" not in resp.text
    assert "secret-detail" in caplog.text
