"""Root-cause inspector CLI — graduated from eval_tools/_langfuse_drill.py.

Graduation note: `--raw` mode absorbs eval_tools/_langfuse_drill.py's timeline
view. The annotated stage-by-stage render is new capability added here.
See eval_tools/_langfuse_drill.py for the original drill script.

Usage
-----
    python -m debug.pipeline <tid>         # annotated stage-by-stage render
    python -m debug.pipeline <tid> --raw   # plain observation timeline

<tid> is either an 8-hex app trace id (resolved via metadata.trace_id) or
a 32-hex Langfuse trace ID.

Known trace: a687e093 = Langfuse 51c47a5061f70aa2 (290s runaway)

Timezone note: Langfuse timestamps are UTC; app.log timestamps are KST (+09:00).

Environment
-----------
Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
(loaded from project/.env by langfuse_client).
"""
from __future__ import annotations

import argparse
import sys

from ._query import get_trace_detail, require_env, resolve_tid

# ── annotation map: what wrong looks like at each stage → suspect module ──────

_STAGE_NOTES: dict[str, tuple[str, str]] = {
    "summarize_history": (
        "What wrong looks like: history summary embeds wrong prior-session content "
        "→ follow-up questions answered as if a different topic was discussed.",
        "Suspect module: summarize_history (prompt / context-window handling)",
    ),
    "rewrite_query": (
        "What wrong looks like: rewritten query diverges from original intent "
        "→ embedding search retrieves off-topic chunks. "
        "OR: rewrite takes >3s → Ollama cold-start / context pressure.",
        "Suspect module: rewrite_query (QueryAnalysis LLM call)",
    ),
    "search_child_chunks": (
        "What wrong looks like: relevant chunk absent from results "
        "→ embedding mismatch (dense bge-m3 / sparse bm25 hybrid fusion). "
        "OR: score_threshold=0.3 gate filtered the chunk → use `repro search --threshold X`.",
        "Suspect module: embedding relevancy / chunking (→ repro search / repro chunk)",
    ),
    "retrieve_parent_chunks": (
        "What wrong looks like: parent document empty or truncated "
        "→ document-reading gap (parent_store miss).",
        "Suspect module: document-reading (→ repro parent <parent_id>)",
    ),
    "orchestrator": (
        "What wrong looks like: high LLM call count (>4) or token count (>4k) "
        "→ agent loop blowup. Context size growing each iteration is the signal.",
        "Suspect module: orchestrator (loop-depth / tool-call cap MAX_TOOL_CALLS)",
    ),
    "compress_context": (
        "What wrong looks like: compress_context present → context was near-limit; "
        "if chunked aggressively, downstream answer may miss key facts.",
        "Suspect module: compress_context (context-window management)",
    ),
    "aggregate_answers": (
        "What wrong looks like: >60s here → LLM repetition loop producing 10k+ char answer. "
        "OR: answer says 'not found' despite results>0 → generation failure (sentinel).",
        "Suspect module: aggregate_answers (→ repro answer '<question>')",
    ),
    "fallback_response": (
        "What wrong looks like: this span exists → the fallback path fired; "
        "check why the main path was skipped.",
        "Suspect module: fallback_response (condition that triggers fallback)",
    ),
}

# ── timeline render ───────────────────────────────────────────────────────────

def _fmt_lat(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds >= 60:
        return f"{seconds:.1f}s ({seconds/60:.1f}min)"
    return f"{seconds:.2f}s"


def _extract_text(val: object, max_chars: int = 200) -> str:
    """Best-effort string extraction from a Langfuse input/output value."""
    if val is None:
        return "(none)"
    if isinstance(val, str):
        text = val
    elif isinstance(val, dict):
        # Common patterns: {"content": "..."}, {"output": "..."}, {"text": "..."}
        text = (
            val.get("content")
            or val.get("output")
            or val.get("text")
            or val.get("result")
            or repr(val)
        )
        if not isinstance(text, str):
            text = repr(val)
    elif isinstance(val, list):
        # List of messages or chunks — take first item content
        if val and isinstance(val[0], dict):
            text = val[0].get("content") or repr(val[0])
        else:
            text = repr(val)
    else:
        text = repr(val)
    if len(text) > max_chars:
        return text[:max_chars] + f"… [+{len(text)-max_chars}ch]"
    return text


def render_raw(trace: dict) -> None:
    """Plain observation timeline (--raw mode; absorbs eval_tools/_langfuse_drill.py)."""
    obs = sorted(
        trace.get("observations", []),
        key=lambda o: o.get("startTime") or "",
    )
    meta = trace.get("metadata") or {}
    tid = meta.get("trace_id", "")
    print(f"\n{'='*72}")
    print(
        f"TRACE  {_fmt_lat(trace.get('latency'))}  "
        f"tid={tid}  id={trace.get('id','')[:16]}  "
        f"sess={str(trace.get('sessionId') or '')[:8]}"
    )
    q = _extract_text(trace.get("input"), 80)
    print(f"  input : {q}")
    n_gen = sum(1 for o in obs if o.get("type") == "GENERATION")
    n_search = sum(1 for o in obs if o.get("name") == "search_child_chunks")
    n_parent = sum(1 for o in obs if o.get("name") == "retrieve_parent_chunks")
    print(f"  LLM calls={n_gen}  searches={n_search}  parent_retrieves={n_parent}")
    print("  timeline (name | type | latency):")
    for o in obs:
        la = _fmt_lat(o.get("latency"))
        lvl = o.get("level") or "DEFAULT"
        lvl_tag = f" [{lvl}]" if lvl != "DEFAULT" else ""
        print(
            f"    {str(o.get('name') or ''):28} "
            f"{str(o.get('type') or ''):12} "
            f"{la:>9}{lvl_tag}"
        )


def render_annotated(trace: dict) -> None:
    """Annotated stage-by-stage pipeline render."""
    obs = sorted(
        trace.get("observations", []),
        key=lambda o: o.get("startTime") or "",
    )
    meta = trace.get("metadata") or {}
    tid = meta.get("trace_id", "")
    total_lat = trace.get("latency")

    print(f"\n{'='*72}")
    print(
        f"PIPELINE INSPECTION\n"
        f"  tid       : {tid}\n"
        f"  langfuse  : {trace.get('id','')}\n"
        f"  session   : {str(trace.get('sessionId') or '')[:8]}\n"
        f"  total     : {_fmt_lat(total_lat)}\n"
        f"  Timezone  : Langfuse=UTC  app.log=KST(+9)"
    )

    # ── STAGE 0: Original question ────────────────────────────────────────────
    question = _extract_text(trace.get("input"), 200)
    print(f"\n{'─'*60}")
    print(f"  [0] QUESTION")
    print(f"      {question}")

    # ── iterate observations in time order ────────────────────────────────────
    search_idx = 0
    orch_idx = 0
    for o in obs:
        name = o.get("name") or ""
        otype = o.get("type") or ""
        lat = o.get("latency")
        inp = o.get("input")
        out = o.get("output")
        lvl = o.get("level") or "DEFAULT"
        tok_total = o.get("totalTokens")
        tok_prompt = o.get("promptTokens")

        if name == "summarize_history":
            print(f"\n{'─'*60}")
            print(f"  [SUMMARIZE_HISTORY]  {_fmt_lat(lat)}")
            print(f"      output: {_extract_text(out, 150)}")
            _print_note(name)

        elif name == "rewrite_query":
            print(f"\n{'─'*60}")
            print(f"  [REWRITE_QUERY]  {_fmt_lat(lat)}")
            print(f"      output (rewritten query): {_extract_text(out, 150)}")
            _print_note(name)

        elif name == "search_child_chunks":
            search_idx += 1
            print(f"\n{'─'*60}")
            print(f"  [SEARCH #{search_idx}]  {_fmt_lat(lat)}")
            # Input is usually the search query dict
            if isinstance(inp, dict):
                q_text = inp.get("query") or inp.get("input") or _extract_text(inp, 80)
            else:
                q_text = _extract_text(inp, 80)
            print(f"      query  : {q_text}")
            # Output: list of (doc, score) or similar
            if isinstance(out, list):
                print(f"      chunks : {len(out)} returned")
                for i, chunk in enumerate(out[:3]):
                    if isinstance(chunk, dict):
                        score = chunk.get("score", chunk.get("_score", "?"))
                        content = _extract_text(
                            chunk.get("content") or chunk.get("page_content") or chunk, 80
                        )
                        src = chunk.get("metadata", {}).get("source", "") if isinstance(
                            chunk.get("metadata"), dict
                        ) else ""
                        print(f"        [{i}] score={score}  src={src}  {content}")
            else:
                print(f"      output : {_extract_text(out, 120)}")
            _print_note(name)

        elif name == "retrieve_parent_chunks":
            print(f"\n{'─'*60}")
            print(f"  [RETRIEVE_PARENT]  {_fmt_lat(lat)}")
            print(f"      output: {_extract_text(out, 120)}")
            _print_note(name)

        elif name == "orchestrator" and otype == "GENERATION":
            orch_idx += 1
            flag = " ⚠ LOOP" if orch_idx > 1 else ""
            print(f"\n{'─'*60}")
            print(f"  [ORCHESTRATOR call #{orch_idx}]  {_fmt_lat(lat)}{flag}")
            if tok_prompt:
                print(f"      context: {tok_prompt} prompt tokens  total: {tok_total}")
            # Check for tool calls in output
            if isinstance(out, dict):
                tool_calls = out.get("tool_calls") or []
                if tool_calls:
                    print(f"      tool calls: {len(tool_calls)}")
                    for tc in tool_calls[:3]:
                        if isinstance(tc, dict):
                            print(f"        → {tc.get('name','?')}({str(tc.get('args',''))[:60]})")
            if lvl != "DEFAULT":
                print(f"      ⚠ level={lvl}: {o.get('statusMessage','')[:80]}")
            _print_note(name)

        elif name == "compress_context":
            print(f"\n{'─'*60}")
            print(f"  [COMPRESS_CONTEXT]  {_fmt_lat(lat)}")
            print(f"      output: {_extract_text(out, 120)}")
            _print_note(name)

        elif name == "aggregate_answers":
            print(f"\n{'─'*60}")
            print(f"  [AGGREGATE_ANSWERS]  {_fmt_lat(lat)}")
            answer_text = _extract_text(out, 300)
            print(f"      answer: {answer_text}")
            if tok_total:
                print(f"      tokens: {tok_total}")
            if lat and lat > 60:
                print(f"      ⚠ RUNAWAY: {_fmt_lat(lat)} — check for LLM repetition loop")
            _print_note(name)

        elif name == "fallback_response":
            print(f"\n{'─'*60}")
            print(f"  [FALLBACK_RESPONSE]  {_fmt_lat(lat)}")
            print(f"      output: {_extract_text(out, 120)}")
            _print_note(name)

    # ── FINAL: answer from trace output ───────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  [FINAL ANSWER]  total={_fmt_lat(total_lat)}")
    answer = _extract_text(trace.get("output"), 300)
    print(f"      {answer}")

    # ── summary flags ─────────────────────────────────────────────────────────
    _print_flags(trace, obs)


def _print_note(stage: str) -> None:
    note = _STAGE_NOTES.get(stage)
    if note:
        print(f"      ┄ {note[0]}")
        print(f"      ┄ {note[1]}")


def _print_flags(trace: dict, obs: list[dict]) -> None:
    """Print anomaly flags at the bottom of the annotated render."""
    flags = []
    total_lat = trace.get("latency") or 0
    if total_lat > 60:
        flags.append(f"⚠ RUNAWAY: total={_fmt_lat(total_lat)}")

    n_orch = sum(1 for o in obs if o.get("name") == "orchestrator" and o.get("type") == "GENERATION")
    if n_orch > 4:
        flags.append(f"⚠ AGENT LOOP: {n_orch} orchestrator calls")

    gen_per_trace = sum(1 for o in obs if o.get("type") == "GENERATION")
    if gen_per_trace > 6:
        flags.append(f"⚠ HIGH LLM CALL COUNT: {gen_per_trace} generations")

    errors = [o for o in obs if (o.get("level") or "DEFAULT") != "DEFAULT"]
    if errors:
        flags.append(f"⚠ NON-DEFAULT observations: {len(errors)}")

    agg = next((o for o in obs if o.get("name") == "aggregate_answers"), None)
    if agg and (agg.get("latency") or 0) > 60:
        flags.append(f"⚠ AGGREGATE BLOWUP: {_fmt_lat(agg.get('latency'))}")

    if flags:
        print(f"\n{'='*72}")
        print("ANOMALY FLAGS:")
        for f in flags:
            print(f"  {f}")
    else:
        print(f"\n{'='*72}")
        print("No anomaly flags detected.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(
        prog="python -m debug.pipeline",
        description=(
            "Root-cause inspector: render a Langfuse trace stage-by-stage.\n\n"
            "Graduated from eval_tools/_langfuse_drill.py (--raw mode);\n"
            "annotated mode is new capability.\n\n"
            "<tid> accepts:\n"
            "  • 8-hex app trace id (e.g. a687e093) — resolved via metadata.trace_id\n"
            "  • Langfuse trace ID — ≥12 hex chars (e.g. 51c47a5061f70aa2 = 16 hex)\n\n"
            "Timezone: Langfuse=UTC, app.log=KST(+09:00).\n\n"
            "Requires: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tid", help="8-hex app tid or 32-hex Langfuse trace ID")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Plain observation timeline (no annotations; absorbs old 'drill')",
    )
    args = parser.parse_args(argv)

    require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    # Resolve tid → 32-hex Langfuse ID
    raw_tid = args.tid.strip().lower()
    print(f"Resolving tid={raw_tid!r} …", file=sys.stderr)
    try:
        trace_id_32 = resolve_tid(raw_tid)
    except (ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  → {trace_id_32}", file=sys.stderr)

    print("Fetching trace detail …", file=sys.stderr)
    try:
        trace = get_trace_detail(trace_id_32)
    except RuntimeError as exc:
        print(
            f"error: could not fetch trace {trace_id_32!r}: {exc}\n"
            "Check LANGFUSE_BASE_URL in project/.env — EU data requires the EU endpoint.",
            file=sys.stderr,
        )
        return 1
    if not trace:
        print(f"error: trace {trace_id_32!r} not found", file=sys.stderr)
        return 1

    if args.raw:
        render_raw(trace)
    else:
        render_annotated(trace)

    return 0


if __name__ == "__main__":
    sys.exit(main())
