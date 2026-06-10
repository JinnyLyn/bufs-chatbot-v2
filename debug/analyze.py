"""Fleet stats CLI — graduated from eval_tools/_langfuse_analyze.py.

Graduation note: this module absorbs the full fleet-stat and per-node
observation analysis from eval_tools/_langfuse_analyze.py, using the shared
query layer (debug/_query.py) instead of duplicating the REST calls.
See eval_tools/_langfuse_analyze.py for the original standalone script.

Usage
-----
    python -m debug.analyze                      # last 7d fleet stats
    python -m debug.analyze --last 50            # last 50 traces only
    python -m debug.analyze --since 2026-06-08   # since ISO date/datetime
    python -m debug.analyze --errors             # non-DEFAULT observations
    python -m debug.analyze --list-nodes         # list all observed node names
    python -m debug.analyze --node rewrite_query # one node's history

Environment
-----------
Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
(loaded from project/.env by langfuse_client).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from .langfuse_client import fetch_observations, fetch_traces
from ._query import fetch_observations_by_name, fetch_traces_window, require_env


# ── percentile helper (mirrors eval_tools/_langfuse_analyze.py) ──────────────

def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


# ── fleet stats ───────────────────────────────────────────────────────────────

def fleet_stats(traces: list[dict], obs: list[dict]) -> None:
    """Print fleet-level latency, node stats, tokens, and error tally."""
    sep = "=" * 70

    # ── 1. trace latency distribution ─────────────────────────────────────────
    lat = [t["latency"] for t in traces if t.get("latency")]
    print(
        f"\n{sep}\nTRACE LATENCY (s): n={len(lat)}"
        + (
            f"  p50={_pct(lat,.5):.1f}  p90={_pct(lat,.9):.1f}"
            f"  p95={_pct(lat,.95):.1f}  max={max(lat):.1f}  min={min(lat):.1f}"
            if lat else "  (no latency data)"
        )
    )
    if lat:
        slow = sorted(traces, key=lambda t: -(t.get("latency") or 0))[:6]
        print("  slowest traces:")
        for t in slow:
            tid = (t.get("metadata") or {}).get("trace_id", "")
            tid_tag = f" tid={tid}" if tid else ""
            print(
                f"    {t.get('latency',0):6.1f}s  "
                f"sess={str(t.get('sessionId') or '')[:8]}  "
                f"{str(t.get('input') or '')[:50]}"
                f"{tid_tag}"
            )

    # ── 2. observations by type/name: latency + tokens ────────────────────────
    by: dict = defaultdict(lambda: {"lat": [], "tok": [], "n": 0})
    errors: list = []
    for o in obs:
        k = (o.get("type"), o.get("name"))
        g = by[k]
        g["n"] += 1
        if o.get("latency"):
            g["lat"].append(o["latency"])
        if o.get("totalTokens"):
            g["tok"].append(o["totalTokens"])
        if (o.get("level") or "DEFAULT") != "DEFAULT":
            errors.append(
                (o.get("level"), o.get("name"), o.get("statusMessage"), o.get("traceId"))
            )

    print(f"\n{sep}\nOBSERVATIONS by type/name (latency s, tokens):")
    for k, g in sorted(by.items(), key=lambda x: -sum(x[1]["lat"] or [0])):
        la, to = g["lat"], g["tok"]
        print(
            f"  {str(k[0]):10} {str(k[1] or '')[:26]:26} n={g['n']:4}  "
            f"lat p50={_pct(la,.5):5.1f} p90={_pct(la,.9):5.1f} p95={_pct(la,.95):5.1f} "
            f"max={max(la) if la else 0:5.1f}  "
            f"tok max={max(to) if to else '-'}"
        )

    # ── 3. errors / warnings ──────────────────────────────────────────────────
    print(f"\n{sep}\nNON-DEFAULT level observations: {len(errors)}")
    lvl = Counter(e[0] for e in errors)
    print("  by level:", dict(lvl))
    for e in errors[:20]:
        print(f"    [{e[0]}] {str(e[1] or '')[:20]:20} {str(e[2] or '')[:90]}")

    # ── 4. agent-loop depth: LLM generations per trace ────────────────────────
    gen_per_trace: Counter = Counter()
    for o in obs:
        if o.get("type") == "GENERATION":
            gen_per_trace[o.get("traceId") or "unknown"] += 1
    if gen_per_trace:
        vals = list(gen_per_trace.values())
        print(f"\n{sep}\nLLM CALLS PER TRACE (agent-loop depth):")
        import statistics
        print(
            f"  mean={statistics.mean(vals):.1f}  "
            f"p50={_pct(vals,.5):.0f}  p90={_pct(vals,.9):.0f}  max={max(vals)}"
        )
        print("  distribution:", dict(sorted(Counter(vals).items())))


# ── node history ──────────────────────────────────────────────────────────────

def node_history(name: str, obs: list[dict]) -> None:
    """Print one node's execution history (latency, tokens, errors)."""
    node_obs = [o for o in obs if o.get("name") == name]
    if not node_obs:
        print(f"  No observations found for node '{name}'.")
        print(
            "  Tip: 'retrieve_parent_chunks', 'compress_context', 'fallback_response' "
            "were unobserved in 200 production traces — this path may be inactive."
        )
        return

    sep = "=" * 70
    lat = [o["latency"] for o in node_obs if o.get("latency")]
    tok = [o["totalTokens"] for o in node_obs if o.get("totalTokens")]
    print(f"\n{sep}\nNODE: {name}  (n={len(node_obs)})")
    if lat:
        print(
            f"  latency (s): p50={_pct(lat,.5):.2f}  p90={_pct(lat,.9):.2f}"
            f"  p95={_pct(lat,.95):.2f}  max={max(lat):.2f}  min={min(lat):.2f}"
        )
    if tok:
        print(
            f"  tokens:      p50={_pct(tok,.5):.0f}  p90={_pct(tok,.9):.0f}"
            f"  max={max(tok)}"
        )
    errors = [o for o in node_obs if (o.get("level") or "DEFAULT") != "DEFAULT"]
    print(f"  errors/warnings: {len(errors)}")

    # Recent executions
    recent = sorted(node_obs, key=lambda o: o.get("startTime") or "", reverse=True)[:20]
    print(f"\n  Recent {len(recent)} executions:")
    for o in recent:
        ts = (o.get("startTime") or "")[:19]
        la = f"{o['latency']:.2f}s" if o.get("latency") else "-"
        tk = str(o.get("totalTokens") or "-")
        lvl = o.get("level") or "DEFAULT"
        lvl_tag = f" [{lvl}]" if lvl != "DEFAULT" else ""
        tid_meta = (o.get("metadata") or {})
        tid = tid_meta.get("trace_id", str(o.get("traceId") or "")[:8])
        print(f"    {ts}  tid={tid}  lat={la}  tok={tk}{lvl_tag}")


# ── list nodes ────────────────────────────────────────────────────────────────

def list_nodes(obs: list[dict]) -> None:
    """Print all observed node names with observation counts."""
    counts: Counter = Counter()
    for o in obs:
        name = o.get("name") or "(unnamed)"
        counts[name] += 1
    print("Observed nodes (count):")
    for name, n in counts.most_common():
        print(f"  {n:5}  {name}")

    # Flag known pipeline nodes that were NOT observed
    known_nodes = [
        "rewrite_query", "orchestrator", "search_child_chunks",
        "aggregate_answers", "summarize_history", "tools",
        "collect_answer", "agent",
        "retrieve_parent_chunks", "compress_context", "fallback_response",
    ]
    missing = [n for n in known_nodes if n not in counts]
    if missing:
        print("\n⚠  Known pipeline nodes with ZERO observations (paths may be inactive):")
        for n in missing:
            print(f"    {n}")


# ── error summary ─────────────────────────────────────────────────────────────

def error_summary(obs: list[dict]) -> None:
    """Print all non-DEFAULT level observations."""
    errors = [o for o in obs if (o.get("level") or "DEFAULT") != "DEFAULT"]
    if not errors:
        print("No non-DEFAULT observations found in this window.")
        return
    print(f"Non-DEFAULT observations ({len(errors)} total):")
    for o in sorted(errors, key=lambda x: x.get("startTime") or ""):
        ts = (o.get("startTime") or "")[:19]
        tid = (o.get("metadata") or {}).get("trace_id", str(o.get("traceId") or "")[:8])
        print(
            f"  [{o.get('level')}] {ts}  tid={tid}  "
            f"node={str(o.get('name') or '')[:24]}  "
            f"msg={str(o.get('statusMessage') or '')[:80]}"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(
        prog="python -m debug.analyze",
        description=(
            "Fleet stats from Langfuse Cloud EU.\n\n"
            "Graduated from eval_tools/_langfuse_analyze.py — uses the shared\n"
            "query layer (debug/_query.py) for consistent pagination.\n\n"
            "Requires: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL\n"
            "          (loaded from project/.env)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node", metavar="NAME",
        help="Show one node's execution history (absorbs old 'module' tool)")
    parser.add_argument("--errors", action="store_true",
        help="Show all non-DEFAULT level observations")
    parser.add_argument("--last", metavar="N", type=int,
        help="Limit to last N traces (overrides --since window)")
    parser.add_argument("--since", metavar="ISO",
        help="Since ISO date/datetime, e.g. 2026-06-08 or 2026-06-08T12:00:00")
    parser.add_argument("--list-nodes", action="store_true",
        help="List all observed node names with counts")

    args = parser.parse_args(argv)

    # Validate credentials exist
    require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    # Determine fetch window
    days = 7
    want = 200
    extra_filters: dict = {}

    if args.since:
        # Explicit range overrides the default 7d window (see fetch_traces_window)
        extra_filters["fromTimestamp"] = args.since
        want = 500  # wider when given explicit range
    if args.last:
        want = args.last

    traces = fetch_traces_window(days=days, want=want, **extra_filters)
    print(f"Pulled {len(traces)} traces", end="", flush=True)

    # ~10 obs/trace typical; cap at 1200 for full fleet, scale down for --last N
    obs_want = min(max(len(traces) * 10, 100), 1200)
    obs = fetch_observations(want=obs_want)
    print(f", {len(obs)} observations")

    if args.list_nodes:
        list_nodes(obs)
    elif args.errors:
        error_summary(obs)
    elif args.node:
        node_history(args.node, obs)
    else:
        fleet_stats(traces, obs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
