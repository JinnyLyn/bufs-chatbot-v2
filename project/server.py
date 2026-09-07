"""FastAPI entrypoint — agentic-RAG core behind the CamChat chat UI.

Run:  python project/server.py   (or: uvicorn server:app --app-dir project)

The shared RAGSystem is built once at startup. Because Qdrant runs in embedded
(local-file) mode, only one process may hold the DB at a time — finish ingestion
(`python project/ingest.py ...`) before starting this server.
"""

import logging
import os
import sys

# Windows consoles default to cp949 here; the pipeline prints ✓/emoji chars. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# Persistent + trace-aware logging: stdout + daily-rotating file, every line prefixed
# with the request's [trace_id]. (Also silences the benign OTel detach warning.)
from api.log_setup import configure_logging

_LOG_PATH = configure_logging()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import chat as chat_router
from api import health as health_router
from api import session as session_router
from api.runtime import get_runtime_info, init_rag_system

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🔨 Initializing agentic-RAG system (LLM + embeddings + Qdrant + graph)...")
    logger.info("log file: %s", _LOG_PATH)
    init_rag_system()
    import config as _config
    if _config.RERANK_ENABLED:
        import time as _time
        logger.info("🔨 Pre-loading reranker model (%s) on %s...", _config.RERANK_MODEL, _config.RERANK_DEVICE)
        _t0 = _time.perf_counter()
        from db import reranker as _reranker_mod
        _reranker_mod.get_reranker()
        logger.info("✅ Reranker ready in %.1fs.", _time.perf_counter() - _t0)
    logger.info("🚀 RAG system ready. Serving. runtime=%s", get_runtime_info())
    yield
    # No explicit Langfuse flush here: the SDK registers its own atexit shutdown
    # (which flushes), and a flush against an unreachable host would stall
    # uvicorn's graceful-shutdown window for ~30s.


# Interactive API docs are a live request-builder against an unauthenticated API and
# advertise every route, parameter and schema. They are a development convenience, so
# they stay off unless explicitly asked for. (The tunnel only forwards ^/api/, so this
# mainly hardens direct-to-origin access.) Set ENABLE_DOCS=true for local development.
_docs_enabled = os.getenv("ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="Agentic RAG × CamChat",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

_cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The frontend reads Retry-After on 503/429 to lock its retry button. Same-origin
    # (the production path: cloudflared routes /api here) does not need this; it only
    # matters if a deployment ever serves the API cross-origin, which today also
    # requires relaxing the frontend CSP's connect-src 'self'.
    expose_headers=["Retry-After"],
)

app.include_router(session_router.router)
app.include_router(chat_router.router)
app.include_router(health_router.router)


@app.get("/")
async def root():
    body = {"message": "Agentic RAG × CamChat API"}
    if _docs_enabled:
        body["docs"] = "/docs"
    return body


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    # Bind loopback-only by default. Every consumer dials localhost — cloudflared's
    # ingress (service: http://localhost:8000), scripts/healthcheck.sh and the eval
    # harness — so the public path is the tunnel and nothing else. Binding 0.0.0.0
    # additionally published this unauthenticated API (plus /docs and /health) to the
    # whole LAN subnet, bypassing Cloudflare's TLS, WAF and rate limiting. The frontend
    # already pins HOSTNAME=127.0.0.1 in scripts/start-all.sh; this matches it.
    # Override with HOST=0.0.0.0 only behind a firewall that blocks the port.
    host = os.getenv("HOST", "127.0.0.1")
    # The chat endpoint carries the user's question in the query string, so uvicorn's
    # access log would write every student's question — and their IP — to a second
    # file with different retention and no redaction than the Q&A log. The application
    # already logs each request as [chat-IN]/[chat-OUT] with a trace id and a truncated
    # question, which is what debugging actually needs. Set ACCESS_LOG=true to restore.
    access_log = os.getenv("ACCESS_LOG", "").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run(app, host=host, port=port, access_log=access_log)
