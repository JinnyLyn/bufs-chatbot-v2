"""Single-shot server monitor — cron-able, non-zero exit on any anomaly.

Usage
-----
    python -m debug.status

Exit codes (documented):
    0   all checks pass, no anomalies detected
    1   one or more anomaly conditions detected (see output for details)
    2   configuration error (missing required env var)

Anomaly conditions that trigger exit 1:
    • /health endpoint unreachable or returns non-200
    • Langfuse error/WARNING observations in the last 7 days
    • Trailing 7d median latency has degraded relative to the prior 7d
    • chat-IN-without-chat-OUT orphan(s) older than the grace period in the
      recent app.log tail (sub-grace orphans are DEFERRED to the next run)
    • Any known pipeline node has ZERO observations in the last 7 days

Environment
-----------
BUFS_SERVER_URL     — server base URL (default: http://localhost:8000)
BUFS_LOG_DIR        — log tree root for orphan detection (default: logs/)
LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
                    — loaded from project/.env by langfuse_client

Cron example (Linux):
    */5 * * * * cd /path/to/repo && .venv/bin/python -m debug.status >> /var/log/bufs-status.log 2>&1

Windows Task Scheduler alert delivery:
    Action: Start a program
    Program: python  Arguments: -m debug.status
    Working dir: C:\\path\\to\\repo
    On failure (exit code 1): add a second action to send an email or call a webhook:
      powershell -Command "Invoke-WebRequest -Uri 'https://hooks.slack.com/...' -Method POST -Body '{\"text\":\"BUFS status anomaly\"}'"
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests as _requests

# dotenv runs via ensure_env() in main() ONLY — importing stays env-pure
from .langfuse_client import ensure_env, fetch_observations
from ._query import fetch_observations_by_name, fetch_traces_window
from .logs import grep_app_logs, iter_app_log_files

# ── orphan grace period ───────────────────────────────────────────────────────

# Sub-grace chat-IN-without-chat-OUT pairs are likely still in-flight rather
# than crashed. 300s > the 60s RUNAWAY threshold in logs.py and covers the
# observed 290s runaway request, so a long-but-live request is not mis-flagged.
ORPHAN_GRACE_SECONDS = 300


def _utc_since(days: int) -> str:
    """RFC 3339 timestamp for ``now - days`` (UTC), matching _query's format."""
    return (
        (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        + "+00:00"
    )

# ── known pipeline nodes to check for liveness ───────────────────────────────

# Nodes observed in production (should appear in any healthy 7d window):
_EXPECTED_NODES = [
    "rewrite_query",
    "orchestrator",
    "search_child_chunks",
    "aggregate_answers",
]

# Nodes that have never been observed in 200 production traces — flag if
# they appear (unexpected), or note their absence is expected:
_KNOWN_INACTIVE = [
    "retrieve_parent_chunks",   # 0 obs in 200 traces (2026-06-10 analysis)
    "compress_context",         # 0 obs in 200 traces
    "fallback_response",        # 0 obs in 200 traces
]

_ALL_KNOWN_NODES = _EXPECTED_NODES + _KNOWN_INACTIVE + [
    "summarize_history", "tools", "collect_answer", "agent", "LangGraph",
]


# ── health check ──────────────────────────────────────────────────────────────

def check_health(server_url: str) -> tuple[bool, str]:
    """GET /health and return (ok, detail).

    /health returns liveness + runtime config ONLY.
    Error/WARNING counts and latency come from Langfuse + qa.jsonl — not /health.
    """
    url = server_url.rstrip("/") + "/health"
    try:
        r = _requests.get(url, timeout=10)
        if r.status_code == 200:
            config_keys = list((r.json() or {}).keys()) if r.headers.get(
                "content-type", ""
            ).startswith("application/json") else []
            detail = f"HTTP 200  config_keys={config_keys}"
            return True, detail
        return False, f"HTTP {r.status_code}"
    except _requests.exceptions.ConnectionError:
        return False, f"connection refused ({url})"
    except _requests.exceptions.Timeout:
        return False, f"timeout ({url})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── latency baseline (rolling window, NOT hardcoded) ─────────────────────────

def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def check_latency(recent_traces: list[dict], prior_traces: list[dict]) -> tuple[bool, str]:
    """Compare recent 7d median latency vs prior 7d median.

    Returns (ok, detail). Degraded if recent median > 1.5× prior median
    (with a minimum prior of 3s to avoid false positives from sparse windows).
    """
    recent_lat = [t["latency"] for t in recent_traces if t.get("latency")]
    prior_lat = [t["latency"] for t in prior_traces if t.get("latency")]

    r_med = _median(recent_lat)
    p_med = _median(prior_lat)

    if r_med is None:
        return False, "no latency data in recent 7d window"
    if p_med is None or p_med < 3.0:
        # Can't establish baseline; report recent stats only
        return True, f"recent p50={r_med:.1f}s  (no prior baseline — window may be sparse)"

    ratio = r_med / p_med
    detail = (
        f"recent 7d p50={r_med:.1f}s  prior 7d p50={p_med:.1f}s  "
        f"ratio={ratio:.2f}"
    )
    if ratio > 1.5:
        return False, f"DEGRADED: {detail}"
    return True, detail


# ── error / warning check ─────────────────────────────────────────────────────

def check_errors(obs: list[dict]) -> tuple[bool, str]:
    """Check for non-DEFAULT level observations in the recent window."""
    errors = [o for o in obs if (o.get("level") or "DEFAULT") != "DEFAULT"]
    if not errors:
        return True, "no non-DEFAULT observations"
    by_level: dict[str, int] = {}
    for o in errors:
        lvl = o.get("level") or "UNKNOWN"
        by_level[lvl] = by_level.get(lvl, 0) + 1
    return False, f"{len(errors)} non-DEFAULT obs: {by_level}"


# ── node-path liveness check ──────────────────────────────────────────────────

def check_node_liveness(obs: list[dict]) -> tuple[bool, list[str]]:
    """Check which expected nodes have 0 observations in the window.

    Returns (ok, detail_lines). ok=False if any _EXPECTED_NODES are absent.
    """
    from collections import Counter
    counts: Counter = Counter()
    for o in obs:
        name = o.get("name") or ""
        if name:
            counts[name] += 1

    lines = []
    anomaly = False

    for node in sorted(_ALL_KNOWN_NODES):
        n = counts.get(node, 0)
        if node in _KNOWN_INACTIVE:
            if n > 0:
                tag = f"  ⚠ UNEXPECTED (was 0 in 200 production traces)"
            else:
                tag = "  (expected absent — path inactive in production)"
        elif node in _EXPECTED_NODES:
            if n == 0:
                tag = "  ⚠ ABSENT — path may be broken"
                anomaly = True
            else:
                tag = f"  n={n}"
        else:
            tag = f"  n={n}"
        lines.append(f"    {node:28} {tag}")

    return (not anomaly), lines


# ── orphan detection from local app.log tail ─────────────────────────────────

def check_orphans(tail_lines: int = 500) -> tuple[bool, list[str]]:
    """Detect chat-IN without matching chat-OUT in the recent log tail.

    This is the ONLY crash/abort signal app.log provides — no ERROR lines,
    no tracebacks are ever written to app.log.

    A chat-IN within ORPHAN_GRACE_SECONDS of the newest parseable log line is
    likely still in-flight, not crashed, so it is DEFERRED (re-evaluated on the
    next run) rather than flagged — this avoids a false cron exit 1 for a slow
    request. Only chat-INs older than the grace with no chat-OUT are real
    ORPHANs. The reference clock is the newest parseable line timestamp in the
    tail (deterministic), NOT wall-clock now().

    Reads the last *tail_lines* log lines from the active app.log.
    """
    from .logs import _log_root, parse_line

    log_root = _log_root()
    app_log = log_root / "backend" / "app.log"
    if not app_log.exists():
        return True, ["app.log not found — skipping orphan detection"]

    # Read tail
    try:
        with open(app_log, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except OSError as exc:
        return True, [f"could not read app.log: {exc}"]

    tail = all_lines[-tail_lines:]
    in_ts: dict[str, datetime] = {}   # tid → latest chat-IN timestamp
    out_tids: set[str] = set()
    newest_ts: datetime | None = None

    for raw in tail:
        line = parse_line(raw)
        if not line or line.tid == "-":
            continue
        ts = datetime.strptime(line.timestamp, "%Y-%m-%d %H:%M:%S,%f")
        if newest_ts is None or ts > newest_ts:
            newest_ts = ts
        if "[chat-IN]" in line.message:
            # keep the latest chat-IN timestamp for this tid
            if line.tid not in in_ts or ts > in_ts[line.tid]:
                in_ts[line.tid] = ts
        elif "[chat-OUT]" in line.message:
            out_tids.add(line.tid)

    candidates = [tid for tid in in_ts if tid not in out_tids]
    if not candidates or newest_ts is None:
        return True, [f"no orphans in last {len(tail)} log lines"]

    orphans: list[str] = []
    deferred: list[str] = []
    for tid in candidates:
        age = (newest_ts - in_ts[tid]).total_seconds()
        if age < ORPHAN_GRACE_SECONDS:
            deferred.append(tid)
        else:
            orphans.append(tid)

    if not orphans:
        msgs = [f"no orphans in last {len(tail)} log lines"]
        if deferred:
            msgs.append(
                f"deferred {len(deferred)} tid(s) within {ORPHAN_GRACE_SECONDS}s "
                "grace (may be in-flight)"
            )
        return True, msgs

    msgs = [
        f"⚠ {len(orphans)} ORPHAN(S) detected in last {len(tail)} lines "
        f"(chat-IN without chat-OUT, older than {ORPHAN_GRACE_SECONDS}s grace "
        "— possible crash/abort):"
    ]
    for tid in sorted(orphans):
        msgs.append(f"    tid={tid}")
    if deferred:
        msgs.append(
            f"deferred {len(deferred)} tid(s) within {ORPHAN_GRACE_SECONDS}s "
            "grace (may be in-flight)"
        )
    return False, msgs


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ensure_env()  # load project/.env before parser defaults + credential check
    parser = argparse.ArgumentParser(
        prog="python -m debug.status",
        description=(
            "Single-shot server health monitor.\n\n"
            "Exit codes:\n"
            "  0  all checks pass\n"
            "  1  one or more anomalies detected\n"
            "  2  configuration error\n\n"
            "Sources:\n"
            "  /health           — liveness + runtime config ONLY\n"
            "  Langfuse REST     — error counts, latency baseline (rolling 7d)\n"
            "  app.log tail      — chat-IN/OUT orphan detection (the ONLY crash signal)\n\n"
            "Cron (Linux):\n"
            "  */5 * * * * cd /repo && .venv/bin/python -m debug.status >> /var/log/bufs.log\n\n"
            "Windows Task Scheduler alert delivery:\n"
            "  On failure (exit 1), trigger a second action:\n"
            "    powershell -Command \"Invoke-WebRequest -Uri 'https://hooks.slack.com/...' "
            "-Method POST -Body '{\\\"text\\\":\\\"BUFS status anomaly\\\"}'\"  \n"
            "  Or email via Send-MailMessage / a local SMTP relay.\n\n"
            "Requires: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL\n"
            "          (loaded from project/.env by langfuse_client)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("BUFS_SERVER_URL", "http://localhost:8000"),
        help="Server base URL (default: $BUFS_SERVER_URL or http://localhost:8000)",
    )
    args = parser.parse_args(argv)

    # Validate Langfuse credentials
    missing = [k for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
               if not os.environ.get(k)]
    if missing:
        print(
            f"error: missing environment variable(s): {', '.join(missing)}\n"
            "       Set them in project/.env (see .env.example)",
            file=sys.stderr,
        )
        return 2

    anomalies: list[str] = []
    print(f"BUFS Server Status Check")
    print(f"{'='*60}")

    # ── 1. health endpoint ────────────────────────────────────────────────────
    ok, detail = check_health(args.server_url)
    tag = "✓" if ok else "✗"
    print(f"\n[{tag}] /health   {detail}")
    if not ok:
        anomalies.append(f"/health: {detail}")

    # ── 2. fetch Langfuse data ────────────────────────────────────────────────
    print("\nFetching Langfuse data (recent 7d + prior 7d) …", flush=True)
    langfuse_ok = True
    recent_traces: list[dict] = []
    prior_only: list[dict] = []
    obs: list[dict] = []
    try:
        # Time-bound the two windows so newest-first pagination can't starve the
        # prior window: recent = [now-7d, now], prior = [now-14d, now-7d).
        # Bounding by toTimestamp (not a fragile ID set-difference) is the fix —
        # truncated recent traces can no longer leak into the prior baseline.
        cutoff_7d = _utc_since(days=7)
        recent_traces = fetch_traces_window(days=7, want=300)
        prior_traces = fetch_traces_window(days=14, want=600, toTimestamp=cutoff_7d)
        # Secondary guard: drop any recent IDs that still slipped into prior.
        recent_ids = {t.get("id") for t in recent_traces}
        prior_only = [t for t in prior_traces if t.get("id") not in recent_ids]
        # Windowless obs leaks pre-7d data into error/node-liveness; bound to 7d.
        # NB: the observations REST endpoint filters on `fromStartTime` (the
        # traces endpoint uses `fromTimestamp`); using the wrong name no-ops.
        obs = fetch_observations(want=1200, fromStartTime=cutoff_7d)
        print(f"    Pulled: {len(recent_traces)} recent traces, {len(prior_only)} prior, {len(obs)} obs")
    except RuntimeError as exc:
        print(f"[✗] Langfuse fetch failed: {exc}")
        print("    Check LANGFUSE_BASE_URL in project/.env (EU data requires EU endpoint).")
        anomalies.append(f"Langfuse unreachable: {exc}")
        langfuse_ok = False

    # ── 3-5. Langfuse-derived checks (skip gracefully if fetch failed) ────────
    if langfuse_ok:
        ok, detail = check_latency(recent_traces, prior_only)
        tag = "✓" if ok else "✗"
        print(f"\n[{tag}] latency   {detail}")
        if not ok:
            anomalies.append(f"latency: {detail}")

        ok, detail = check_errors(obs)
        tag = "✓" if ok else "✗"
        print(f"\n[{tag}] errors    {detail}")
        if not ok:
            anomalies.append(f"errors: {detail}")

        ok, lines = check_node_liveness(obs)
        tag = "✓" if ok else "✗"
        print(f"\n[{tag}] node liveness:")
        for ln in lines:
            print(ln)
        if not ok:
            absent = [ln.strip() for ln in lines if "ABSENT" in ln]
            anomalies.append(f"dead nodes: {', '.join(absent)}")
    else:
        print("\n[─] latency / errors / node liveness  (skipped — Langfuse unavailable)")

    # ── 6. orphan detection ───────────────────────────────────────────────────
    ok, msgs = check_orphans()
    tag = "✓" if ok else "✗"
    print(f"\n[{tag}] orphan detection:")
    for m in msgs:
        print(f"    {m}")
    if not ok:
        anomalies.extend(msgs)

    # ── summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if anomalies:
        print(f"STATUS: ANOMALY ({len(anomalies)} issue(s))")
        for a in anomalies:
            print(f"  ⚠ {a}")
        print("\nExit code 1 — trigger alert delivery (see --help for cron/Task Scheduler examples)")
        return 1
    else:
        print("STATUS: OK — no anomalies detected")
        return 0


if __name__ == "__main__":
    sys.exit(main())
