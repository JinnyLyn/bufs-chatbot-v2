"""Session inspector CLI — show all Q&As in a session with verdict flags.

Usage
-----
    python -m debug.session <session_id|tid>

Accepts (auto-detected by format):
  • a full Langfuse session UUID            → fetched directly
  • an 8-hex app trace id                   → resolved to its session
  • a 12–40-hex Langfuse trace ID           → fetched, then its session
    (the production format is 16-hex; pure-hex 12–40 is always read as a
    Langfuse trace ID, so partial-UUID-prefix lookup is not supported)

Verdict flags per Q&A
---------------------
  REFUSE       refuse-path signature: no retrieval + no tools + agent≈0ms
  NO-RESULTS   num_results == 0
  SENTINEL     answer says "찾지 못했습니다" but retrieval ran (generation failure)
  RUNAWAY      answer length > 5000 chars OR total duration > 60s
  ORPHAN       [chat-IN] found but no [chat-OUT] in app.log (crash/abort signal)

Timezone note: Langfuse=UTC, app.log=KST(+09:00).

Environment
-----------
Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
(loaded from project/.env by langfuse_client).
BUFS_LOG_DIR  — log tree root for orphan detection (default: logs/).
"""
from __future__ import annotations

import argparse
import re
import sys

from ._query import (
    fetch_session_traces,
    get_trace_detail,
    require_env,
    resolve_tid,
)
from .logs import grep_app_logs

# Sentinel substring — "제공된 자료에서...찾지 못했습니다"
_SENTINEL = "찾지 못했습니다"

# ── verdict detection ─────────────────────────────────────────────────────────

def _verdicts(trace: dict, obs: list[dict]) -> list[str]:
    """Return a list of verdict flag strings for one trace."""
    flags: list[str] = []
    meta = trace.get("metadata") or {}

    # Extract key stats from trace metadata / observations
    lat = trace.get("latency") or 0  # seconds

    # Try to get num_results and answer from metadata or output
    num_results = meta.get("num_results")
    tool_calls_count = meta.get("tool_calls")
    answer_text = ""
    if isinstance(trace.get("output"), str):
        answer_text = trace["output"]
    elif isinstance(trace.get("output"), dict):
        answer_text = str(trace["output"].get("content") or trace["output"].get("output") or "")

    # REFUSE: no retrieval path. Fires only on an explicit tool_calls == 0 —
    # a missing metadata key (None) means "unknown", not "refused".
    search_obs = [o for o in obs if o.get("name") == "search_child_chunks"]
    if not search_obs and tool_calls_count == 0:
        flags.append("REFUSE")

    # NO-RESULTS
    if num_results == 0:
        flags.append("NO-RESULTS")

    # SENTINEL: answer says not-found but search ran
    if _SENTINEL in answer_text and search_obs:
        flags.append("SENTINEL")

    # RUNAWAY
    if lat > 60:
        flags.append(f"RUNAWAY({lat:.0f}s)")
    if answer_text and len(answer_text) > 5000:
        flags.append(f"RUNAWAY-ANSWER({len(answer_text)}ch)")

    return flags


def _check_orphan(tid: str) -> bool:
    """True if this tid has a chat-IN but no chat-OUT in local logs."""
    if not tid:
        return False
    app_lines = grep_app_logs(tid)
    has_in = any("[chat-IN]" in ln.message for ln in app_lines)
    has_out = any("[chat-OUT]" in ln.message for ln in app_lines)
    return has_in and not has_out


# ── display ───────────────────────────────────────────────────────────────────

def _fmt_lat(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds >= 60:
        return f"{seconds:.1f}s ⚠RUNAWAY"
    return f"{seconds:.1f}s"


def display_session(traces: list[dict]) -> None:
    """Display all Q&As in a session with verdicts."""
    if not traces:
        print("  No traces found for this session.")
        return

    # Sort by timestamp
    traces_sorted = sorted(traces, key=lambda t: t.get("timestamp") or "")

    session_id = str(traces_sorted[0].get("sessionId") or "")
    print(f"\n{'='*70}")
    print(f"SESSION: {session_id}")
    print(f"  Q&As: {len(traces_sorted)}")
    print(f"  Timezone: Langfuse=UTC, app.log=KST(+09:00)")
    print(f"{'='*70}\n")

    for i, t in enumerate(traces_sorted, 1):
        meta = t.get("metadata") or {}
        tid = meta.get("trace_id", "")
        ts = (t.get("timestamp") or "")[:19]
        lat = t.get("latency")
        lf_id = t.get("id", "")

        # Fetch full detail for observations (one REST call per trace — show
        # progress so large sessions don't look hung)
        print(
            f"  [{i:02d}/{len(traces_sorted)}] fetching trace detail …",
            file=sys.stderr,
        )
        try:
            detail = get_trace_detail(lf_id)
            obs = detail.get("observations", [])
            answer_text = ""
            if isinstance(detail.get("output"), str):
                answer_text = detail["output"]
            elif isinstance(detail.get("output"), dict):
                answer_text = str(
                    detail["output"].get("content")
                    or detail["output"].get("output")
                    or ""
                )
        except Exception:
            detail = t
            obs = []
            answer_text = ""

        flags = _verdicts(detail, obs)
        orphan = _check_orphan(tid) if tid else False
        if orphan:
            flags.append("ORPHAN")

        flag_str = "  ".join(f"[{f}]" for f in flags) if flags else "OK"

        question = ""
        inp = t.get("input") or detail.get("input")
        if isinstance(inp, str):
            question = inp[:100]
        elif isinstance(inp, dict):
            question = str(inp.get("question") or inp.get("content") or inp)[:100]

        print(f"  [{i:02d}] {ts}  tid={tid or lf_id[:8]}  {_fmt_lat(lat)}")
        print(f"       Q: {question}")
        if answer_text:
            preview = answer_text[:150] + "…" if len(answer_text) > 150 else answer_text
            print(f"       A: {preview}")
        print(f"       verdict: {flag_str}")

        # Show observation summary for this trace
        n_search = sum(1 for o in obs if o.get("name") == "search_child_chunks")
        n_orch = sum(1 for o in obs if o.get("name") == "orchestrator"
                     and o.get("type") == "GENERATION")
        if obs:
            agg = next((o for o in obs if o.get("name") == "aggregate_answers"), None)
            agg_lat = f"  aggregate={_fmt_lat(agg.get('latency'))}" if agg else ""
            print(
                f"       stats: searches={n_search}  orchestrator_calls={n_orch}"
                f"{agg_lat}"
            )
        print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(
        prog="python -m debug.session",
        description=(
            "Show all Q&As in a session with per-question verdict flags.\n\n"
            "Scenario entry point: identify the bad answer in a session, then\n"
            "use `debug.pipeline <tid>` to drill into the failing request.\n\n"
            "Verdict flags:\n"
            "  REFUSE       — refuse path (no retrieval, no tools)\n"
            "  NO-RESULTS   — search returned 0 results\n"
            "  SENTINEL     — answer says 'not found' despite retrieval running\n"
            "  RUNAWAY      — duration >60s or answer >5000 chars\n"
            "  ORPHAN       — chat-IN in app.log with no matching chat-OUT\n\n"
            "Timezone: Langfuse=UTC, app.log=KST(+09:00).\n\n"
            "Requires: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "session_or_tid",
        help=(
            "full Langfuse session UUID, 8-hex app tid, or 12-40-hex "
            "Langfuse trace ID (each resolved to its session). Pure-hex "
            "12-40 is read as a Langfuse trace ID; UUID-prefix lookup is "
            "unsupported."
        ),
    )
    args = parser.parse_args(argv)

    require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    raw = args.session_or_tid.strip()

    # Determine input format: session UUID, 8-hex app tid, or Langfuse trace ID.
    # _hex_lf covers the production 16-hex format AND web-UI 32-hex; keep the
    # 12-40 bound in parity with _query.resolve_tid() / launcher._HEX_LANGFUSE.
    _hex8 = re.compile(r"^[0-9a-fA-F]{8}$")
    _uuid = re.compile(
        r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", re.I
    )
    _hex_lf = re.compile(r"^[0-9a-fA-F]{12,40}$")

    # All Langfuse calls below can raise (network / auth / malformed response).
    # Catch and exit 1 with a readable message (mirrors pipeline.py main()).
    try:
        if _uuid.match(raw):
            # Full session UUID
            session_id = raw
            print(f"Fetching session {session_id[:8]}… …", file=sys.stderr)
            traces = fetch_session_traces(session_id)
        elif _hex8.match(raw):
            # 8-hex app tid — resolve to Langfuse id, then get session.
            # Checked before _hex_lf: 8 is outside [12,40] so no overlap, but
            # explicit ordering keeps the app-tid path unambiguous.
            print(f"Resolving tid={raw!r} …", file=sys.stderr)
            try:
                lf_id = resolve_tid(raw.lower())
            except (ValueError, LookupError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"  → {lf_id}", file=sys.stderr)
            detail = get_trace_detail(lf_id)
            session_id = str(detail.get("sessionId") or "")
            print(f"  → session {session_id[:8]}", file=sys.stderr)
            traces = fetch_session_traces(session_id)
        elif _hex_lf.match(raw):
            # 12-40-hex Langfuse trace ID (prod is 16-hex) — fetch the trace to
            # get its sessionId, then fetch the session's traces. Routing the
            # RESOLVED sessionId (not the raw trace id) into fetch_session_traces
            # is the fix for bug #5.
            print(f"Resolving Langfuse trace {raw[:16]}… …", file=sys.stderr)
            detail = get_trace_detail(raw)
            session_id = str(detail.get("sessionId") or "")
            print(f"  → session {session_id[:8]}", file=sys.stderr)
            traces = fetch_session_traces(session_id)
        else:
            # Treat as partial session id prefix — fetch with sessionId filter
            print(f"Fetching session prefix={raw!r} …", file=sys.stderr)
            traces = fetch_session_traces(raw)
    except (RuntimeError, ValueError, KeyError, ConnectionError) as exc:
        print(f"error: failed to fetch session data: {exc}", file=sys.stderr)
        return 1

    print(f"Found {len(traces)} trace(s) in session.", file=sys.stderr)
    display_session(traces)
    return 0


if __name__ == "__main__":
    sys.exit(main())
