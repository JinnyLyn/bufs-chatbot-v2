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
