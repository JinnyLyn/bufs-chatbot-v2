"""Process-wide RAG runtime: a single RAGSystem shared across all sessions.

The agentic-RAG graph compiles one in-memory checkpointer. We key every web
session to its own LangGraph *thread* (thread_id == session_id), so a single
compiled graph serves many independent multi-turn conversations.
"""

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import config
from core.rag_system import RAGSystem

_rag_system: Optional[RAGSystem] = None
_init_lock = threading.Lock()
_START_TS = time.monotonic()

# Lightweight session registry. The real conversation memory lives in the
# graph checkpointer (keyed by thread_id); this only tracks UI-facing metadata.
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def init_rag_system() -> RAGSystem:
    """Build + initialize the shared RAGSystem once (called from app startup)."""
    global _rag_system
    with _init_lock:
        if _rag_system is None:
            rs = RAGSystem()
            rs.initialize()
            _rag_system = rs
    return _rag_system


def get_rag_system() -> RAGSystem:
    if _rag_system is None:
        raise RuntimeError("RAG system not initialized — call init_rag_system() first.")
    return _rag_system


def build_config(session_id: str, trace_id: str = "-") -> dict:
    """LangGraph run config for a session. session_id is used as the thread_id.

    `metadata` carries Langfuse trace grouping keys so each turn shows up under its
    session in the Langfuse dashboard.
    """
    rs = get_rag_system()
    cfg = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": rs.recursion_limit,
        "metadata": {"langfuse_session_id": session_id, "trace_id": trace_id},
    }
    handler = rs.observability.get_handler()
    if handler:
        cfg["callbacks"] = [handler]
    return cfg


def get_runtime_info() -> dict:
    """Config snapshot for the /health endpoint — makes the active model and (crucially)
    which Ollama endpoint is in use unambiguous (local :11435 vs the remote tunnel)."""
    try:
        kb_docs = len(list(Path(config.MARKDOWN_DIR).glob("*.md")))
    except Exception:
        kb_docs = -1
    return {
        "model": config.LLM_MODEL,
        "ollama_base_url": config.OLLAMA_BASE_URL or f"OLLAMA_HOST={os.environ.get('OLLAMA_HOST', 'default')}",
        "num_ctx": config.LLM_NUM_CTX,
        "reasoning": config.LLM_REASONING,
        "embedding_model": config.DENSE_MODEL,
        "embedding_device": config.EMBEDDING_DEVICE,
        "langfuse_enabled": config.LANGFUSE_ENABLED,
        "kb_docs": kb_docs,
        "uptime_s": int(time.monotonic() - _START_TS),
    }


def _session_info(session_id: str, lang: str) -> dict:
    """Shape expected by the frontend `SessionInfo` type (auxiliary fields nulled)."""
    return {
        "session_id": session_id,
        "lang": lang if lang in ("ko", "en") else "ko",
        "user_profile": None,
        "has_transcript": False,
        "messages_count": 0,
    }


def create_session(lang: str = "ko") -> dict:
    info = _session_info(str(uuid.uuid4()), lang)
    with _sessions_lock:
        _sessions[info["session_id"]] = info
    return info


def get_session(session_id: str) -> Optional[dict]:
    with _sessions_lock:
        return _sessions.get(session_id)


def delete_session(session_id: str) -> bool:
    """세션 메타데이터를 삭제 (로그아웃용). 삭제됐으면 True.

    LangGraph 체크포인터의 대화 메모리는 별도로, 프론트가 로그아웃 후 새 session_id를
    발급받으므로 이전 thread에는 더 이상 접근하지 않는다.
    """
    with _sessions_lock:
        return _sessions.pop(session_id, None) is not None


def ensure_session(session_id: str, lang: str = "ko") -> dict:
    """Return existing session metadata, or register it on the fly.

    The chat stream may arrive for a session_id the server didn't mint (e.g. after
    a server restart while the browser kept its id). We accept it rather than 404.
    """
    with _sessions_lock:
        info = _sessions.get(session_id)
        if info is None:
            info = _session_info(session_id, lang)
            _sessions[session_id] = info
        return info
