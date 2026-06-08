"""Per-request trace_id via ContextVar (ported from CamChat's backend/trace_context.py).

One chat request flows through router → graph (summarize → rewrite → agent(tools) →
aggregate). Attaching the same short trace_id to every log line makes the whole flow
reconstructable with a single `grep <trace_id> logs/backend/app.log`.

NOTE: ContextVars do NOT propagate into worker threads. The agent runs in a thread
(see api/chat.py), so call set_trace_id(tid) again as the first line inside that thread.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

# default "-" means "no trace_id" (server startup / background jobs).
_trace_id_var: ContextVar[str] = ContextVar("agentic_rag_trace_id", default="-")


def new_trace_id() -> str:
    """Short, greppable 8-hex trace id."""
    return uuid.uuid4().hex[:8]


def set_trace_id(tid: str) -> None:
    _trace_id_var.set(tid)


def get_trace_id() -> str:
    return _trace_id_var.get()


class TraceFilter(logging.Filter):
    """Inject `trace_id` onto every LogRecord. Include `%(trace_id)s` in the format
    string to get an automatic [trace_id] prefix on every line."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_var.get()
        return True
