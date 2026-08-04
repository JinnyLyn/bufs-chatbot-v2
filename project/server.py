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
from api import user as user_router
from api.runtime import get_runtime_info, init_rag_system
from db.user_db import init_db as init_user_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🔨 Initializing agentic-RAG system (LLM + embeddings + Qdrant + graph)...")
    logger.info("log file: %s", _LOG_PATH)
    init_user_db()  # 계정/이력 SQLite — 가볍고 RAG 초기화와 무관하므로 먼저 띄운다
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


app = FastAPI(title="Agentic RAG × CamChat", version="0.1.0", lifespan=lifespan)

_cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(session_router.router)
app.include_router(chat_router.router)
app.include_router(health_router.router)
app.include_router(user_router.router)


@app.get("/")
async def root():
    return {"message": "Agentic RAG × CamChat API", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
