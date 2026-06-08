"""Health + LLM/GPU monitoring endpoints.

GET /health      → runtime config snapshot (model, which Ollama endpoint, KB size, uptime)
GET /health/llm  → queries the configured Ollama's /api/ps for loaded model + GPU offload %

Sync `def` routes so FastAPI runs them in a threadpool (the requests call won't block the loop).
"""

import logging
import os

import requests
from fastapi import APIRouter

import config
from api.runtime import get_runtime_info

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


def _ollama_base() -> str:
    """Resolve the Ollama base URL the LLM client actually uses."""
    if config.OLLAMA_BASE_URL:
        base = config.OLLAMA_BASE_URL
    else:
        base = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
        if not base.startswith("http"):
            base = "http://" + base
    return base.rstrip("/")


@router.get("/health")
def health():
    """Liveness + runtime config (which model / Ollama endpoint / KB size)."""
    return {"status": "ok", **get_runtime_info()}


@router.get("/health/llm")
def health_llm():
    """Loaded models + GPU offload % from the configured Ollama (local :11435 vs remote)."""
    base = _ollama_base()
    try:
        resp = requests.get(f"{base}/api/ps", timeout=5)
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "ollama_base_url": base, "error": str(exc)}

    loaded = []
    for m in models:
        size = m.get("size", 0) or 0
        vram = m.get("size_vram", 0) or 0
        loaded.append({
            "name": m.get("name"),
            "size_mb": round(size / 1048576, 1),
            "vram_mb": round(vram / 1048576, 1),
            "gpu_offload_pct": round(100 * vram / size, 1) if size else 0,
        })
    return {"status": "ok", "ollama_base_url": base, "loaded_models": loaded}
