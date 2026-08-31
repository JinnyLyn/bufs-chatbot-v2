"""Process-wide RAG runtime: a single RAGSystem shared across all sessions.

The agentic-RAG graph compiles one in-memory checkpointer. We key every web
session to its own LangGraph *thread* (thread_id == session_id), so a single
compiled graph serves many independent multi-turn conversations.
"""

import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import config
from core.rag_system import RAGSystem

logger = logging.getLogger(__name__)

_rag_system: Optional[RAGSystem] = None
_init_lock = threading.Lock()
_START_TS = time.monotonic()

# Every session id this server hands out is a uuid4 (create_session), and every client
# — the web UI, scripts/healthcheck.sh, and the eval/KPI harnesses — obtains its id from
# POST /api/session. So the id space can be pinned to the UUID grammar. This blocks
# guessable ids ("test", "admin", "1") that would otherwise let one caller land on
# another caller's LangGraph thread, and it stops arbitrary attacker text from becoming
# a dict key and a Langfuse session label.
_SESSION_ID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# Cap on the session registry. Unbounded, it grew by one entry per distinct id an
# anonymous caller supplied, so a loop over fresh uuid4s was a free memory-exhaustion
# primitive against a server with no authentication. Oldest-first eviction: losing an
# entry costs only UI metadata, since ensure_session re-registers on the next turn.
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "10000"))

# Lightweight session registry. The real conversation memory lives in the
# graph checkpointer (keyed by thread_id); this only tracks UI-facing metadata.
_sessions: "OrderedDict[str, dict]" = OrderedDict()
_sessions_lock = threading.Lock()


def is_valid_session_id(session_id: str) -> bool:
    """True when `session_id` is a UUID, the only shape this server issues."""
    return bool(session_id and _SESSION_ID_RE.match(session_id))


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


def build_config(session_id: str) -> dict:
    """LangGraph run config for a session. session_id is used as the thread_id.

    `langfuse_session_id` groups each turn under its session in the Langfuse
    dashboard; the app's log trace_id is stamped trace-wide by
    Observability.chat_turn, so it is not duplicated here.
    """
    rs = get_rag_system()
    cfg = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": rs.recursion_limit,
        "metadata": {"langfuse_session_id": session_id},
    }
    callbacks = rs.observability.langchain_callbacks()
    if callbacks:
        cfg["callbacks"] = callbacks
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


def _drop_thread(session_id: str) -> None:
    """Delete a session's conversation history from the graph checkpointer.

    The checkpointer is a LangGraph InMemorySaver (rag_agent/graph.py), which keeps
    every thread for the life of the process. Evicting only the metadata registry would
    bound the small dict and leave the large structure — the actual messages — growing
    without limit, so eviction has to reach through to the checkpointer as well.

    Never raises: this runs on the request path, and reclaiming memory must not be able
    to fail a user's request. It is also a no-op before the RAG system is initialised.
    """
    try:
        checkpointer = get_rag_system().agent_graph.checkpointer
    except Exception:  # noqa: BLE001 — not initialised (tests, startup) or no graph yet
        return
    try:
        checkpointer.delete_thread(session_id)
    except Exception:  # noqa: BLE001 — unknown thread, or a saver without delete_thread
        logger.debug("could not drop checkpointer thread for evicted session", exc_info=True)


def _register_locked(session_id: str, info: dict) -> list[str]:
    """Insert `info` and evict oldest entries past MAX_SESSIONS. Caller holds the lock.

    Returns the evicted ids so the caller can drop their checkpointer threads outside
    the lock — `delete_thread` touches the graph and should not run under `_sessions_lock`.
    """
    _sessions[session_id] = info
    _sessions.move_to_end(session_id)
    evicted: list[str] = []
    while len(_sessions) > MAX_SESSIONS:
        evicted.append(_sessions.popitem(last=False)[0])
    return evicted


def create_session(lang: str = "ko") -> dict:
    info = _session_info(str(uuid.uuid4()), lang)
    with _sessions_lock:
        evicted = _register_locked(info["session_id"], info)
    for sid in evicted:
        _drop_thread(sid)
    return info


def get_session(session_id: str) -> Optional[dict]:
    with _sessions_lock:
        return _sessions.get(session_id)


def ensure_session(session_id: str, lang: str = "ko") -> dict:
    """Return existing session metadata, or register it on the fly.

    The chat stream may arrive for a session_id the server didn't mint (e.g. after
    a server restart while the browser kept its id). We accept it rather than 404 —
    but only if it is a well-formed UUID, so the id space stays the one we issue.

    Raises:
        ValueError: `session_id` is not a UUID.
    """
    if not is_valid_session_id(session_id):
        raise ValueError("session_id must be a UUID")
    with _sessions_lock:
        info = _sessions.get(session_id)
        if info is None:
            info = _session_info(session_id, lang)
        evicted = _register_locked(session_id, info)
    for sid in evicted:
        _drop_thread(sid)
    return info
