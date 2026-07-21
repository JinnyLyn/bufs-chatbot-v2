"""Shared pytest fixtures for the bufs-chatbot-v2 unit/integration test suite.

Import-time landmine:
  project/config.py executes env reads at module level (constants like MAX_TOOL_CALLS,
  CHAT_LOG_DISABLED, LANGFUSE_ENABLED, etc. are evaluated once when the module is first
  imported). To test different env values you must:
    1. Set env vars BEFORE any import of config (or its dependents).
    2. Use ``importlib.reload(config)`` after patching env with monkeypatch, so the
       module-level constants are recalculated.
    3. Modules that did ``from config import X`` hold stale *bindings* — they must
       also be reloaded or accessed via ``config.X`` for the patched value to be visible.
  The ``env_isolated`` fixture handles cleanup; reload is done per-test as needed.
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------

# Environment variables owned by config.py that should be cleared between
# tests to prevent cross-test contamination via module-level constants.
_CONFIG_ENV_KEYS = (
    "LANGFUSE_ENABLED",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "OLLAMA_HOST",
    "OLLAMA_BASE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "LLM_REASONING",
    "LLM_NUM_CTX",
    "CHAT_LOG_DISABLED",
    "MAX_TOOL_CALLS",
    "MAX_ITERATIONS",
    "BASE_TOKEN_THRESHOLD",
    "MAX_PARENT_SIZE",
    "DENSE_MODEL",
    "EMBEDDING_DEVICE",
    "LOG_BACKUP_DAYS",
    "STRUCTURED_OUTPUT_METHOD",
    "SEARCH_SCORE_THRESHOLD",
    "SLOT_EXTRACTION_ENABLED",
    "SLOT_CLARIFY_ENABLED",
)


@pytest.fixture()
def env_isolated(monkeypatch):
    """Remove all LANGFUSE_*/LLM_*/OLLAMA_* env vars for the duration of a test
    and force-reload config so module-level constants reflect the clean state.

    Usage::

        def test_something(env_isolated, monkeypatch):
            monkeypatch.setenv("LANGFUSE_ENABLED", "true")
            import importlib, config as cfg
            importlib.reload(cfg)
            assert cfg.LANGFUSE_ENABLED is True
    """
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Reload config so its module-level constants reflect the cleared env.
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    yield
    # After the test, reload once more so later tests don't inherit patched
    # values that the monkeypatch teardown has already removed.
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------

class _FakeLLMMessage:
    """Minimal stand-in for langchain AIMessage / HumanMessage."""

    def __init__(self, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    """Callable stub compatible with how rag_agent nodes invoke the LLM.

    By default returns a plain text response with no tool calls. Override
    ``responses`` (a list of ``_FakeLLMMessage``) to control what the fake
    returns on successive calls.
    """

    def __init__(self, responses: list[_FakeLLMMessage] | None = None):
        self._responses = list(responses or [_FakeLLMMessage("fake answer")])
        self._call_count = 0

    def invoke(self, messages: list, **kwargs) -> _FakeLLMMessage:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    def stream(self, messages: list, **kwargs):
        msg = self.invoke(messages, **kwargs)
        yield msg

    def __call__(self, messages: list, **kwargs) -> _FakeLLMMessage:
        return self.invoke(messages, **kwargs)


@pytest.fixture()
def fake_llm():
    """A FakeLLM instance with a default no-tool-call response."""
    return FakeLLM()


@pytest.fixture()
def fake_llm_with_tool_call():
    """A FakeLLM whose first response contains a tool call."""
    tool_call_msg = _FakeLLMMessage(
        content="",
        tool_calls=[{"name": "search_child_chunks", "args": {"query": "test"}, "id": "tc0"}],
    )
    return FakeLLM(responses=[tool_call_msg, _FakeLLMMessage("final answer")])


# ---------------------------------------------------------------------------
# Fake vector / parent store
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_vector_store():
    """MagicMock standing in for a Qdrant-backed vector store.

    Pre-configures ``similarity_search_with_score`` to return an empty list so
    tests that just need the store to exist don't need extra setup.
    """
    store = MagicMock()
    store.similarity_search_with_score.return_value = []
    store.similarity_search.return_value = []
    return store


@pytest.fixture()
def fake_parent_store(tmp_path):
    """A simple dict-backed stand-in for the parent document store.

    The real store is a shelve/SQLite mapping parent_id → Document.  This
    fixture provides the same get/set interface using a plain dict so tests
    remain offline and fast.
    """
    store: dict[str, Any] = {}

    class _FakeStore:
        def __getitem__(self, key):
            return store[key]

        def __setitem__(self, key, value):
            store[key] = value

        def __contains__(self, key):
            return key in store

        def get(self, key, default=None):
            return store.get(key, default)

        def keys(self):
            return store.keys()

        def close(self):
            pass

    return _FakeStore()


# ---------------------------------------------------------------------------
# Fake / disabled Langfuse handler
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_langfuse_handler():
    """A MagicMock Langfuse callback handler.

    All method calls (on_llm_start, on_llm_end, flush, …) are no-ops so tests
    never need Langfuse credentials and never hit the network.
    """
    handler = MagicMock()
    handler.trace_id = "fake-trace-00"
    return handler


# ---------------------------------------------------------------------------
# Logging capture
# ---------------------------------------------------------------------------

@pytest.fixture()
def caplog_info(caplog):
    """Set root logger to INFO for the duration of the test."""
    with caplog.at_level(logging.INFO):
        yield caplog
