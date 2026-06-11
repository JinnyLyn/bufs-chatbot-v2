"""Local log join CLI — grep app.log* + qa_*.jsonl by trace id.

Usage
-----
    python -m debug.logs <tid>

Where <tid> is an 8-hex app trace id (e.g. ``a687e093``).

Environment
-----------
BUFS_LOG_DIR
    Root of the log tree (default: ``logs/`` relative to repo root).
    Expected layout::

        $BUFS_LOG_DIR/backend/app.log
        $BUFS_LOG_DIR/backend/app.log.YYYY-MM-DD
        $BUFS_LOG_DIR/qa/qa_YYYY-MM-DD.jsonl

Notes
-----
- Timestamps in app.log are naive KST (+09:00); Langfuse uses UTC.
- Log grammar is 100% verified against 1,632 real lines — see
  .omc/research/log-study-applog.md.
- ``[chat-ERR]`` (ERROR) and QA-write-failure (WARNING from chat.py:57,
  ERROR from qa_logger.py:78) lines are parsed and displayed. They have 0
  occurrences in the committed corpus (see log-study-code-xcheck.md §2) and
  are tested with synthetic fixtures.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

# ── repo-root anchor; dotenv runs in main() ONLY (importing stays env-pure) ───
from dotenv import load_dotenv as _load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ── constants ─────────────────────────────────────────────────────────────────

# Grammar (verified against 1,632 real lines, 0 non-matches):
#   YYYY-MM-DD HH:MM:SS,mmm [tid|-] LEVEL name:func:lineno - msg
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"  # 1: timestamp
    r" \[([0-9a-f]{8}|-)\]"                             # 2: tid or -
    r" (INFO|WARNING|ERROR|CRITICAL)"                   # 3: level
    r" ([\w.]+):(\w+):(\d+)"                            # 4-6: name:func:lineno
    r" - (.*)$",                                        # 7: message
    re.UNICODE,
)

# [chat-IN] — api.chat:chat_stream:77
# q=%r repr: single-quoted normally, double-quoted when text contains '
CHAT_IN_RE = re.compile(
    r"\[chat-IN\] tid=([0-9a-f]{8}) sid=([0-9a-f]{8})"
    r" q_chars=(\d+) q=(['\"])(.*?)\4 model=(\S+) test=(True|False)",
    re.DOTALL,
)

# [chat-OUT] — api.chat:_finalize:36
CHAT_OUT_RE = re.compile(
    r"\[chat-OUT\] tid=([0-9a-f]{8}) sid=([0-9a-f]{8})"
    r" answer_chars=(\d+) results=(\d+) sources=(\d+) total_ms=(\d+)"
)

# PIPELINE_TIMING — api.chat:_finalize:41 (stage ms values have literal "ms" suffix)
PIPELINE_TIMING_RE = re.compile(
    r"PIPELINE_TIMING tid=([0-9a-f]{8})"
    r" total=(\d+)ms summarize=(\d+)ms rewrite=(\d+)ms"
    r" agent=(\d+)ms aggregate=(\d+)ms other=(\d+)ms"
    r" sub_q=(\d+) tool_calls=(\d+) model=(\S+)"
)

# [chat-ERR] — api.chat:chat_stream:114 via logger.error (ERROR level).
# Parsed now that LINE_RE accepts ERROR/CRITICAL; 0 real occurrences in the
# committed corpus as of 2026-06-10 (synthetic fixture coverage only).
# Format: [chat-ERR] tid=<8hex> <message>
CHAT_ERR_RE = re.compile(r"\[chat-ERR\] tid=([0-9a-f]{8}) (.*)")

# QA-write failure — two sources, both parsed now that LINE_RE accepts ERROR:
#   chat.py:57         logger.warning("Q&A log failed: %s", exc)        → WARNING
#   qa_logger.py:78    logger.error("Q&A log write failed: %s", exc)    → ERROR
# 0 real occurrences in the committed corpus as of 2026-06-10.
QA_LOG_FAIL_RE = re.compile(r"Q&A log (?:write )?failed: (.*)")


# ── log-directory helpers ─────────────────────────────────────────────────────

def _log_root() -> Path:
    """Return the log root directory from BUFS_LOG_DIR or default."""
    env = os.environ.get("BUFS_LOG_DIR", "")
    if env:
        return Path(env)
    return _REPO_ROOT / "logs"


def iter_app_log_files(log_root: Path | None = None) -> list[Path]:
    """Return all app.log* files under <log_root>/backend/, sorted by name.

    The active file ``app.log`` sorts first (lexicographically before any
    dated ``app.log.YYYY-MM-DD`` file, being a prefix of those names).
    """
    root = log_root or _log_root()
    backend = root / "backend"
    if not backend.is_dir():
        return []
    files = sorted(backend.glob("app.log*"))
    return files


def iter_qa_files(log_root: Path | None = None) -> list[Path]:
    """Return all qa_*.jsonl files under <log_root>/qa/, sorted by name."""
    root = log_root or _log_root()
    qa_dir = root / "qa"
    if not qa_dir.is_dir():
        return []
    return sorted(qa_dir.glob("qa_*.jsonl"))


# ── line-level parser ─────────────────────────────────────────────────────────

class LogLine:
    """Parsed app.log record."""

    __slots__ = (
        "raw", "timestamp", "tid", "level",
        "logger", "func", "lineno", "message",
    )

    def __init__(
        self,
        raw: str,
        timestamp: str,
        tid: str,
        level: str,
        logger: str,
        func: str,
        lineno: str,
        message: str,
    ):
        self.raw = raw
        self.timestamp = timestamp
        self.tid = tid
        self.level = level
        self.logger = logger
        self.func = func
        self.lineno = lineno
        self.message = message


def parse_line(raw: str) -> LogLine | None:
    """Parse a raw app.log line into a LogLine.

    Returns None for lines that don't match the known grammar (e.g. old
    httpx ``INFO:`` style or untimestamped uvicorn lines in stderr streams).
    """
    m = LINE_RE.match(raw.rstrip("\r\n"))
    if not m:
        return None
    return LogLine(
        raw=raw.rstrip("\r\n"),
        timestamp=m.group(1),
        tid=m.group(2),
        level=m.group(3),
        logger=m.group(4),
        func=m.group(5),
        lineno=m.group(6),
        message=m.group(7),
    )


def parse_chat_in(msg: str) -> dict | None:
    """Extract fields from a [chat-IN] message.

    Returns dict with keys: tid, sid, q_chars, q, model, test
    or None if the message is not a chat-IN.
    """
    m = CHAT_IN_RE.search(msg)
    if not m:
        return None
    return {
        "tid": m.group(1),
        "sid": m.group(2),
        "q_chars": int(m.group(3)),
        "q": m.group(5),       # repr-decoded (without surrounding quotes)
        "model": m.group(6),
        "test": m.group(7) == "True",
    }


def parse_chat_out(msg: str) -> dict | None:
    """Extract fields from a [chat-OUT] message."""
    m = CHAT_OUT_RE.search(msg)
    if not m:
        return None
    return {
        "tid": m.group(1),
        "sid": m.group(2),
        "answer_chars": int(m.group(3)),
        "results": int(m.group(4)),
        "sources": int(m.group(5)),
        "total_ms": int(m.group(6)),
    }


def parse_pipeline_timing(msg: str) -> dict | None:
    """Extract fields from a PIPELINE_TIMING message.

    Stage values have the literal ``ms`` suffix in the log but are returned
    as plain ints. total is also an int (ms).
    """
    m = PIPELINE_TIMING_RE.search(msg)
    if not m:
        return None
    return {
        "tid": m.group(1),
        "total_ms": int(m.group(2)),
        "summarize_ms": int(m.group(3)),
        "rewrite_ms": int(m.group(4)),
        "agent_ms": int(m.group(5)),
        "aggregate_ms": int(m.group(6)),
        "other_ms": int(m.group(7)),
        "sub_q": int(m.group(8)),
        "tool_calls": int(m.group(9)),
        "model": m.group(10),
    }


def parse_chat_err(msg: str) -> dict | None:
    """Extract fields from a [chat-ERR] message.

    [chat-ERR] is logged at ERROR level (chat.py:114) and is now parsed and
    displayed. 0 such lines exist in the committed corpus as of 2026-06-10;
    synthetic-fixture coverage only.
    """
    m = CHAT_ERR_RE.search(msg)
    if not m:
        return None
    return {"tid": m.group(1), "error": m.group(2)}


def parse_qa_log_fail(msg: str) -> dict | None:
    """Extract the exception text from a QA-write-failure line.

    Matches both the chat.py:57 WARNING ("Q&A log failed: ...") and the
    qa_logger.py:78 ERROR ("Q&A log write failed: ..."). 0 such lines exist
    in the committed corpus as of 2026-06-10.
    """
    m = QA_LOG_FAIL_RE.search(msg)
    if not m:
        return None
    return {"error": m.group(1)}


# ── grep helpers ──────────────────────────────────────────────────────────────

def grep_app_logs(tid: str, log_root: Path | None = None) -> list[LogLine]:
    """Return all app.log lines whose bracket tid matches *tid*.

    Reads every app.log* file under <log_root>/backend/ in sorted order.
    CRLF is handled transparently (strip \\r\\n).
    """
    results: list[LogLine] = []
    for path in iter_app_log_files(log_root):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = parse_line(raw)
                    if line and line.tid == tid:
                        results.append(line)
        except OSError:
            continue
    return results


def grep_qa_logs(tid: str, log_root: Path | None = None) -> list[dict]:
    """Return QA records whose ``trace_id`` matches *tid*.

    Reads every qa_*.jsonl under <log_root>/qa/.
    """
    results: list[dict] = []
    for path in iter_qa_files(log_root):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("trace_id") == tid:
                        results.append(rec)
        except OSError:
            continue
    return results


# ── display ───────────────────────────────────────────────────────────────────

def _fmt_duration(ms: int) -> str:
    if ms >= 60_000:
        return f"{ms / 1000:.1f}s ({ms / 60_000:.1f}min)"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def display_log_lines(lines: list[LogLine]) -> None:
    """Pretty-print app.log lines for a given tid."""
    if not lines:
        print("  (no app.log lines found)")
        return
    for line in lines:
        msg = line.message
        # Classify and annotate
        if "[chat-IN]" in msg:
            fields = parse_chat_in(msg)
            if fields:
                q_note = " [TRUNCATED]" if fields["q_chars"] > 80 else ""
                print(
                    f"  {line.timestamp} [chat-IN ]  "
                    f"q({fields['q_chars']}ch)={fields['q']!r}{q_note}"
                    f"  model={fields['model']}"
                )
            else:
                print(f"  {line.timestamp} [chat-IN ]  {msg}")
        elif "[chat-OUT]" in msg:
            fields = parse_chat_out(msg)
            if fields:
                dur = _fmt_duration(fields["total_ms"])
                flag = " ⚠ RUNAWAY" if fields["total_ms"] > 60_000 else ""
                flag += " ⚠ NO-RESULTS" if fields["results"] == 0 else ""
                print(
                    f"  {line.timestamp} [chat-OUT]  "
                    f"answer={fields['answer_chars']}ch  "
                    f"results={fields['results']}  sources={fields['sources']}  "
                    f"total={dur}{flag}"
                )
            else:
                print(f"  {line.timestamp} [chat-OUT]  {msg}")
        elif "PIPELINE_TIMING" in msg:
            fields = parse_pipeline_timing(msg)
            if fields:
                print(
                    f"  {line.timestamp} [TIMING  ]  "
                    f"total={_fmt_duration(fields['total_ms'])}  "
                    f"rewrite={_fmt_duration(fields['rewrite_ms'])}  "
                    f"agent={_fmt_duration(fields['agent_ms'])}  "
                    f"aggregate={_fmt_duration(fields['aggregate_ms'])}  "
                    f"sub_q={fields['sub_q']}  tool_calls={fields['tool_calls']}"
                )
            else:
                print(f"  {line.timestamp} [TIMING  ]  {msg}")
        elif "[chat-ERR]" in msg:
            fields = parse_chat_err(msg)
            err_text = fields["error"] if fields else msg
            print(f"  {line.timestamp} [chat-ERR] ⚠  {err_text}")
        elif "Q&A log" in msg and "failed" in msg:
            print(f"  {line.timestamp} [QA-FAIL ] ⚠  {msg}")
        else:
            lvl_tag = {"WARNING": "WARNING", "ERROR": "ERROR  ", "CRITICAL": "ERROR  "}.get(line.level, "info   ")
            print(f"  {line.timestamp} [{lvl_tag}]  {line.logger}:{line.func}  {msg[:120]}")


def display_qa_records(records: list[dict]) -> None:
    """Pretty-print QA records for a given tid."""
    if not records:
        print("  (no QA record found — orphaned request or tracing disabled)")
        return
    for rec in records:
        dur = _fmt_duration(rec.get("duration_ms", 0))
        print(f"  timestamp : {rec.get('timestamp')}")
        print(f"  question  : {rec.get('question', '')[:120]}")
        answer = rec.get("answer", "")
        answer_preview = answer[:200] + "…" if len(answer) > 200 else answer
        print(f"  answer    : {answer_preview}")
        print(
            f"  duration  : {dur}  "
            f"results={rec.get('num_results')}  "
            f"tool_calls={rec.get('tool_calls')}  "
            f"sub_q={rec.get('sub_questions')}"
        )
        sources = rec.get("sources", [])
        if sources:
            print(f"  sources   : {', '.join(sources)}")
        timing = rec.get("timing", {})
        if timing:
            parts = "  ".join(
                f"{k}={_fmt_duration(v)}" for k, v in timing.items() if v
            )
            print(f"  timing    : {parts}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    # dotenv here, never at import (pytest hermeticity): BUFS_LOG_DIR may live in .env
    _load_dotenv(_REPO_ROOT / "project" / ".env", override=False)
    parser = argparse.ArgumentParser(
        prog="python -m debug.logs",
        description=(
            "Grep app.log* and qa_*.jsonl by 8-hex trace id.\n\n"
            "Env: BUFS_LOG_DIR — log tree root (default: repo-relative logs/).\n"
            "Timestamps in app.log are KST (+09:00); Langfuse uses UTC."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tid", help="8-hex trace id (e.g. a687e093)")
    parser.add_argument(
        "--log-dir",
        metavar="DIR",
        help="Override BUFS_LOG_DIR for this run",
    )
    args = parser.parse_args(argv)

    tid = args.tid.strip().lower()
    if not (len(tid) == 8 and all(c in "0123456789abcdef" for c in tid)):
        print(f"error: tid must be exactly 8 lowercase hex chars, got {tid!r}", file=sys.stderr)
        return 2

    log_root: Path | None = Path(args.log_dir) if args.log_dir else None

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    print(f"\n{'='*60}")
    print(f" app.log  — tid={tid}")
    print(f"{'='*60}")
    app_lines = grep_app_logs(tid, log_root)
    display_log_lines(app_lines)

    print(f"\n{'='*60}")
    print(f" qa.jsonl — tid={tid}")
    print(f"{'='*60}")
    qa_recs = grep_qa_logs(tid, log_root)
    display_qa_records(qa_recs)

    # Orphan detection
    has_in = any("[chat-IN]" in ln.message for ln in app_lines)
    has_out = any("[chat-OUT]" in ln.message for ln in app_lines)
    if has_in and not has_out:
        print(
            f"\n⚠  ORPHAN: [chat-IN] found but no [chat-OUT] for tid={tid}.\n"
            "   This is the only crash/abort signal app.log provides.\n"
            "   Check server.err for tracebacks around this timestamp."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
