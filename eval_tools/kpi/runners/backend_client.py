"""Backend SSE client — POST /api/session → GET /api/chat/stream.

Parses the final ``done`` event from the chatbot's SSE stream and returns a
:class:`DoneEvent` dataclass.  NO hardcoded hosts — the base URL is always
supplied by the caller (from the active profile or CLI flag).

SSE protocol (from ``project/api/agent_stream.py``):
  * ``event: token``  — streaming answer fragment.
  * ``event: clear``  — wipe previously streamed tokens (language fallback).
  * ``event: done``   — final JSON payload (answer, timing, results, …).
  * ``event: error``  — server-side error string.

``done`` payload shape (``agent_stream.py:139-152``):
    {
      "answer": str,
      "source_urls": list[str],
      "results": list[dict],   # post-agent citation panel (NOT raw top-k)
      "intent": str,
      "duration_ms": int,
      "model": str,
      "sub_questions": int,
      "tool_calls": int,
      "timing": {              # DICT keyed by node bucket (int ms), NOT array
          "summarize_history": int,
          "rewrite_query": int,
          "agent": int,
          "aggregate_answers": int,
          "other": int,
      }
    }

Integration
-----------
Every function in this module makes live HTTP calls.  Tests that call these
functions must be marked ``@pytest.mark.integration``.
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DoneEvent:
    """Parsed payload from the chatbot's ``done`` SSE event.

    ``timing`` is a DICT keyed by node bucket (int ms values), matching the
    ``done.timing`` contract.  ``None`` when the backend is an older version
    that does not emit timing.

    ``results`` is the post-agent rendered citation panel — NOT the raw
    retriever top-k (scores are 0.0, text is truncated).  Do NOT use for
    retrieval-depth recall/mrr.
    """

    answer: str
    duration_ms: int
    timing: Optional[dict[str, int]] = None
    results: list[dict] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    sub_questions: int = 0
    tool_calls: int = 0
    model: str = ""
    intent: str = ""

    @classmethod
    def from_payload(cls, payload: dict) -> "DoneEvent":
        """Construct from the raw ``done`` event JSON dict."""
        timing = payload.get("timing")
        if not isinstance(timing, dict):
            timing = None
        return cls(
            answer=str(payload.get("answer") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            timing=timing,
            results=payload.get("results") or [],
            source_urls=payload.get("source_urls") or [],
            sub_questions=int(payload.get("sub_questions") or 0),
            tool_calls=int(payload.get("tool_calls") or 0),
            model=str(payload.get("model") or ""),
            intent=str(payload.get("intent") or ""),
        )


# ---------------------------------------------------------------------------
# Session + streaming
# ---------------------------------------------------------------------------

def create_session(base_url: str, *, lang: str = "ko", timeout: float = 30.0) -> str:
    """POST ``/api/session`` and return the ``session_id``.

    Parameters
    ----------
    base_url:
        Chatbot base URL, e.g. ``http://localhost:8000``.  Never hardcoded.
    lang:
        Language hint sent to the backend (default ``"ko"``).
    timeout:
        Request timeout in seconds.

    Returns
    -------
    str
        Session ID string.

    Raises
    ------
    requests.HTTPError
        On non-2xx response.
    """
    import requests  # lazy import — this module is integration-only

    resp = requests.post(
        f"{base_url.rstrip('/')}/api/session",
        json={"lang": lang},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def stream_question(
    base_url: str,
    session_id: str,
    question: str,
    *,
    timeout: float = 300.0,
) -> DoneEvent:
    """GET ``/api/chat/stream`` via SSE and return the parsed ``done`` event.

    Streams until the ``done`` event arrives or the connection closes.
    Raises :class:`BackendError` if the server emits an ``event: error``.

    Parameters
    ----------
    base_url:
        Chatbot base URL.  Never hardcoded.
    session_id:
        Session ID from :func:`create_session`.
    question:
        Question text to send.
    timeout:
        SSE streaming timeout in seconds.

    Returns
    -------
    DoneEvent
        Parsed final event payload.

    Raises
    ------
    BackendError
        When the server emits ``event: error``.
    requests.HTTPError
        On non-2xx HTTP status.
    """
    import requests  # lazy import — this module is integration-only

    url = (
        f"{base_url.rstrip('/')}/api/chat/stream?"
        + urllib.parse.urlencode({"session_id": session_id, "question": question})
    )

    done_payload: dict | None = None
    current_event: str | None = None

    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        # Per the SSE spec a single event's data can span multiple consecutive
        # ``data:`` lines (concatenated with "\n"); accumulate them and decode
        # at the blank-line event delimiter, not per line (E1).
        data_buf: list[str] = []
        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            if raw_line.startswith("event:"):
                current_event = raw_line[6:].strip()
            elif raw_line.startswith("data:"):
                data_buf.append(raw_line[5:].lstrip())
            elif raw_line == "":
                if data_buf:
                    payload_str = "\n".join(data_buf)
                    if current_event == "done":
                        try:
                            done_payload = json.loads(payload_str)
                        except json.JSONDecodeError:
                            pass
                    elif current_event == "error":
                        raise BackendError(payload_str)
                data_buf = []
                current_event = None
        # Flush a trailing event that arrived without a final blank-line delimiter.
        if done_payload is None and current_event == "done" and data_buf:
            try:
                done_payload = json.loads("\n".join(data_buf))
            except json.JSONDecodeError:
                pass

    if done_payload is None:
        raise BackendError("SSE stream closed without a 'done' event")

    return DoneEvent.from_payload(done_payload)


def ask(
    base_url: str,
    question: str,
    *,
    lang: str = "ko",
    session_timeout: float = 30.0,
    stream_timeout: float = 300.0,
) -> DoneEvent:
    """Convenience wrapper: create a session then stream a question.

    Parameters
    ----------
    base_url:
        Chatbot base URL.  Never hardcoded.
    question:
        Question text.
    lang:
        Language hint.
    session_timeout:
        Timeout for the session-creation POST.
    stream_timeout:
        Timeout for the SSE stream.

    Returns
    -------
    DoneEvent
    """
    session_id = create_session(base_url, lang=lang, timeout=session_timeout)
    return stream_question(base_url, session_id, question, timeout=stream_timeout)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class BackendError(RuntimeError):
    """Raised when the chatbot backend returns an ``event: error`` payload
    or when the SSE stream closes without a ``done`` event."""
