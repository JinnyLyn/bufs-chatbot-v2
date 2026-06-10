"""Shared Langfuse query layer — consumed by all debug CLIs.

All stat/inspect tools (analyze, pipeline, session, status) import from here
so query logic is never duplicated across modules (Architect mandate).

Public API
----------
fetch_traces_window(days, want, **filters) -> list[dict]
fetch_observations_by_name(name, want)    -> list[dict]
fetch_observations_by_trace(trace_id_32) -> list[dict]
fetch_session_traces(session_id, want)   -> list[dict]
get_trace_detail(trace_id_32)            -> dict
resolve_tid(tid)                         -> str  (8-hex or 32-hex → 32-hex)
require_env(*keys)                       -> None (exit 1 if any key missing)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

from .langfuse_client import (
    ensure_env,
    fetch_observations,
    fetch_trace_detail,
    fetch_traces,
)

__all__ = [
    "fetch_traces_window",
    "fetch_observations_by_name",
    "fetch_observations_by_trace",
    "fetch_session_traces",
    "get_trace_detail",
    "resolve_tid",
    "require_env",
]


def require_env(*keys: str) -> None:
    """Exit 1 with a readable message if any *keys* are missing from the env.

    Loads project/.env first via ensure_env() (never at import time).
    """
    ensure_env()
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        print(
            f"error: missing environment variable(s): {', '.join(missing)}\n"
            "       Set them in project/.env (see .env.example)",
            file=sys.stderr,
        )
        sys.exit(1)


def fetch_traces_window(days: int = 7, want: int = 200, **filters) -> list[dict]:
    """Fetch up to *want* traces from the last *days* days (UTC).

    Passes RFC 3339 ``fromTimestamp`` to the REST API; additional **filters
    are forwarded verbatim (e.g. ``sessionId=``, ``userId=``). A caller-supplied
    ``fromTimestamp`` overrides the *days* window (no duplicate-kwarg TypeError).
    """
    since = (
        (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        + "+00:00"
    )
    filters.setdefault("fromTimestamp", since)
    return fetch_traces(want=want, **filters)


def fetch_observations_by_name(name: str, want: int = 500) -> list[dict]:
    """Fetch observations with a specific node *name* across all traces."""
    return fetch_observations(want=want, name=name)


def fetch_observations_by_trace(trace_id_32: str) -> list[dict]:
    """Fetch all observations belonging to one 32-hex Langfuse trace ID."""
    return fetch_observations(want=500, traceId=trace_id_32)


def fetch_session_traces(session_id: str, want: int = 50) -> list[dict]:
    """Fetch all traces belonging to a Langfuse session (UUID or 8-hex prefix).

    Tries the REST ``sessionId`` filter first; falls back to client-side
    filtering over a broad fetch if the server rejects the filter parameter.
    """
    try:
        return fetch_traces(want=want, sessionId=session_id)
    except RuntimeError:
        # sessionId filter unsupported or invalid; filter client-side
        all_traces = fetch_traces(want=500)
        return [t for t in all_traces if t.get("sessionId") == session_id][:want]


def get_trace_detail(trace_id_32: str) -> dict:
    """Return the full trace detail dict, including inline observations list."""
    return fetch_trace_detail(trace_id_32)


def resolve_tid(tid: str) -> str:
    """Resolve a trace reference to the 32-hex Langfuse trace ID.

    Accepts
    -------
    - 8-hex app tid  — searches traces for ``metadata.trace_id == tid``
      (the join key used by core/observability.py on the production box).
    - 32-hex Langfuse trace ID — returned as-is after validation.

    Raises
    ------
    ValueError   — unrecognisable format
    LookupError  — 8-hex tid not found in the search window
    """
    tid = tid.strip().lower()
    # 8-hex = app trace_id: resolve via metadata.trace_id search
    if len(tid) == 8 and _is_hex(tid):
        return _search_by_app_tid(tid)
    # Longer hex string (12–40 chars): treat as Langfuse trace ID directly.
    # Langfuse trace IDs vary by SDK version; known production format = 16 hex.
    if 12 <= len(tid) <= 40 and _is_hex(tid):
        return tid
    raise ValueError(
        f"tid must be 8-hex (app trace_id) or a Langfuse trace ID (≥12 hex chars); "
        f"got {tid!r}"
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_hex(s: str) -> bool:
    return all(c in "0123456789abcdef" for c in s)


def _search_by_app_tid(tid: str) -> str:
    """Scan recent traces for metadata.trace_id == *tid*.

    Fetches up to 500 recent traces (newest-first); falls back to a plain
    paginated fetch if the date-filtered window returns a REST error.
    """
    # Try date-windowed fetch first (faster for recent traces)
    try:
        traces = fetch_traces_window(days=90, want=500)
    except RuntimeError:
        # fromTimestamp may not be supported or date range too wide; fall back
        traces = fetch_traces(want=500)

    for t in traces:
        meta = t.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("trace_id") == tid:
            return t["id"]
    raise LookupError(
        f"No Langfuse trace found with metadata.trace_id={tid!r}.\n"
        "Possible causes:\n"
        "  • Trace predates the search window (increase fetch want= or days=)\n"
        "  • Credentials in project/.env are wrong (check LANGFUSE_PUBLIC_KEY / SECRET_KEY)\n"
        "  • LANGFUSE_BASE_URL region mismatch (Cloud EU vs US) — check project/.env\n"
        "  • Langfuse tracing was disabled when this request ran (check langfuse_enabled)\n"
        "Tip: pass the Langfuse trace ID directly to bypass this lookup."
    )
