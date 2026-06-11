"""Offline unit tests for the session CLI dispatch (debug/session.py).

No Langfuse credentials, no network: every query symbol imported into
debug.session is monkeypatched, so main() exercises pure dispatch routing.

Regression target: bug #5 — a 16-hex production Langfuse trace ID must be
resolved to its sessionId via get_trace_detail() and the RESOLVED sessionId
(not the raw 16-hex) handed to fetch_session_traces(). Before the fix the
16-hex fell through to the partial-prefix else branch and filtered sessionId
by a trace id, silently returning 0 traces.
"""

from __future__ import annotations

import pytest

import debug.session as session

_UUID = "217ac056-aaaa-bbbb-cccc-121212121212"
_HEX16 = "51c47a5061f70aa2"  # known production Langfuse trace ID length
_HEX8 = "a687e093"
_LF_32 = "51c47a5061f70aa291ce68a70f9407e3"


class _Calls:
    """Record (name, args) for every patched query function."""

    def __init__(self) -> None:
        self.get_trace_detail: list[str] = []
        self.fetch_session_traces: list[str] = []
        self.resolve_tid: list[str] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Patch debug.session's imported query symbols; record their args.

    require_env is neutralised (no .env / network), and display_session is
    stubbed so dispatch is tested in isolation from rendering.
    """
    rec = _Calls()

    def fake_get_trace_detail(trace_id: str) -> dict:
        rec.get_trace_detail.append(trace_id)
        return {"sessionId": _UUID}

    def fake_fetch_session_traces(session_id: str, *a, **k) -> list[dict]:
        rec.fetch_session_traces.append(session_id)
        return []

    def fake_resolve_tid(tid: str) -> str:
        rec.resolve_tid.append(tid)
        return _LF_32

    monkeypatch.setattr(session, "require_env", lambda *keys: None)
    monkeypatch.setattr(session, "get_trace_detail", fake_get_trace_detail)
    monkeypatch.setattr(session, "fetch_session_traces", fake_fetch_session_traces)
    monkeypatch.setattr(session, "resolve_tid", fake_resolve_tid)
    monkeypatch.setattr(session, "display_session", lambda traces: None)
    return rec


def test_16hex_resolves_to_session_then_fetches_resolved_id(calls: _Calls) -> None:
    """Bug #5 regression: 16-hex is a Langfuse trace ID, NOT a session id.

    get_trace_detail must be called with the raw 16-hex; fetch_session_traces
    must then be called with the RESOLVED sessionId — never with the raw hex.
    """
    assert session.main([_HEX16]) == 0
    assert calls.get_trace_detail == [_HEX16]
    assert calls.fetch_session_traces == [_UUID]
    assert _HEX16 not in calls.fetch_session_traces  # the bug, made explicit
    assert calls.resolve_tid == []  # the app-tid path must not run


def test_32hex_routes_identically_to_16hex(calls: _Calls) -> None:
    """The old exactly-32 branch folded into _hex_lf — same routing as 16-hex."""
    assert session.main([_LF_32]) == 0
    assert calls.get_trace_detail == [_LF_32]
    assert calls.fetch_session_traces == [_UUID]


def test_8hex_takes_app_tid_path(calls: _Calls) -> None:
    """8-hex app tid: resolve_tid → get_trace_detail → fetch_session_traces."""
    assert session.main([_HEX8]) == 0
    assert calls.resolve_tid == [_HEX8]
    assert calls.get_trace_detail == [_LF_32]  # the resolved Langfuse id
    assert calls.fetch_session_traces == [_UUID]


def test_full_uuid_fetches_directly(calls: _Calls) -> None:
    """Full session UUID: fetch_session_traces directly; no trace lookup."""
    assert session.main([_UUID]) == 0
    assert calls.fetch_session_traces == [_UUID]
    assert calls.get_trace_detail == []
    assert calls.resolve_tid == []
