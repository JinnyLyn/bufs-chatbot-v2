"""Report rendering + artifact writing for the KPI gate (WS-E).

Turns a :class:`~eval_tools.kpi.gate.GateResult` (plus the run-context STAMP and
an optional :class:`~eval_tools.kpi.real_usage.RealUsageFamily`) into:

* a **machine** JSON report (the full verdict + the stamp embedded verbatim), and
* a **human** markdown report (per-family verdict + reason, gap-to-target, and
  the HEADLINE ``benchmark_real_gap_pp`` when the real-usage suite ran).

PURE: stdlib only, no ``import config``, no network. The only side effect is
:func:`write_artifacts`, which writes the three run artifacts under the
GITIGNORED ``eval_tools/runs/<ts>-<profile>-<shortsha>/`` directory.

Timestamp policy
----------------
This module NEVER calls ``datetime.now()`` (or any wall-clock) at import or
inside :func:`run_dir_name` / :func:`build_report`. The caller (the CLI) stamps
the time once and passes the compact timestamp string IN — keeping the module
deterministic and import-side-effect-free.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .gate import GateResult
from .real_usage import RealUsageFamily

# report.py lives at eval_tools/kpi/report.py → parent.parent = eval_tools/
_EVAL_TOOLS_DIR: Path = Path(__file__).resolve().parent.parent
# GITIGNORED per .gitignore (`eval_tools/runs/`). Never committed.
RUNS_ROOT: Path = _EVAL_TOOLS_DIR / "runs"

_SLUG_RE: re.Pattern[str] = re.compile(r"[^0-9A-Za-z._-]+")


# ── helpers ──────────────────────────────────────────────────────────────────
def _slug(value: Any) -> str:
    """Filesystem-safe token (alnum/dot/dash/underscore), never empty."""
    return _SLUG_RE.sub("-", str(value)).strip("-") or "x"


def _to_jsonable(obj: Any) -> Any:
    """Recursively turn tuples into lists so JSON round-trips compare equal."""
    if isinstance(obj, Mapping):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def run_dir_name(timestamp: str, profile: str, short_sha: str) -> str:
    """``<ts>-<profile>-<shortsha>`` — all three tokens slugged for the FS.

    ``timestamp`` is a caller-supplied string (e.g. ``20260624T123456Z``); this
    function never reads the clock.
    """
    return f"{_slug(timestamp)}-{_slug(profile)}-{_slug(short_sha)}"


# ── machine report ────────────────────────────────────────────────────────────
def build_report(
    gate_result: GateResult,
    run_context: Mapping[str, Any],
    *,
    real_usage: Optional[RealUsageFamily | Mapping[str, Any]] = None,
    sources: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Assemble the machine-JSON report dict.

    Embeds the full run-context STAMP verbatim (AC#7), serializes every
    per-family verdict + reason, surfaces the accuracy gap-to-target, and — when
    the real-usage suite ran — the HEADLINE ``benchmark_real_gap_pp``.
    """
    families: list[dict[str, Any]] = []
    for fam in gate_result.families:
        families.append(
            {
                "name": fam.name,
                "status": fam.status,
                "reasons": list(fam.reasons),
                "details": _to_jsonable(fam.details),
            }
        )

    # Gap-to-target lives in the accuracy family details (target_contains − current).
    headline: dict[str, Any] = {}
    accuracy = gate_result.family("accuracy")
    if accuracy is not None:
        details = accuracy.details
        if details.get("target_gap") is not None:
            headline["target_gap"] = details["target_gap"]
        if details.get("contains_rate") is not None:
            headline["contains_rate"] = details["contains_rate"]

    real_usage_block: Optional[dict[str, Any]] = None
    if real_usage is not None:
        real_usage_block = (
            real_usage.as_dict() if isinstance(real_usage, RealUsageFamily) else dict(real_usage)
        )
        gap = real_usage_block.get("headline", {}).get("benchmark_real_gap_pp")
        if gap is None:
            gap = real_usage_block.get("benchmark_real_gap_pp")
        headline["benchmark_real_gap_pp"] = gap

    return {
        "verdict": gate_result.verdict,
        "exit_code": gate_result.exit_code,
        "advisory": gate_result.advisory,
        "gating": gate_result.gating,
        "banner": gate_result.banner,
        "n_runs": gate_result.n_runs,
        "headline": headline,
        "families": families,
        "aggregated": _to_jsonable(gate_result.aggregated),
        "real_usage": real_usage_block,
        "sources": list(sources or []),
        "run_context": _to_jsonable(run_context),  # full STAMP, embedded verbatim
    }


# ── human report (markdown) ────────────────────────────────────────────────────
def _fmt(value: Any) -> str:
    """Compact human formatting (floats → 3dp, None → '—')."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the machine report dict to a human-readable markdown document."""
    ctx = report.get("run_context", {})
    lines: list[str] = []

    lines.append(f"# KPI Gate Report — {report['verdict']} (exit {report['exit_code']})")
    lines.append("")
    if report.get("banner"):
        lines.append(f"> {report['banner']}")
        lines.append("")

    # Run context (the stamp).
    lines.append("## Run context")
    lines.append("")
    lines.append("| field | value |")
    lines.append("| --- | --- |")
    for key in (
        "profile", "gating", "machine", "gen_model", "git_sha", "num_ctx",
        "fast_refuse", "compress_threshold", "testset_hash", "scorer_version",
        "scorer_hash", "temp", "seed", "N", "timestamp",
    ):
        if key in ctx:
            lines.append(f"| {key} | {_fmt(ctx.get(key))} |")
    lines.append(f"| n_runs | {report.get('n_runs')} |")
    lines.append("")

    # Headline metrics.
    headline = report.get("headline", {})
    if headline:
        lines.append("## Headline")
        lines.append("")
        if "contains_rate" in headline:
            lines.append(f"- contains_rate: **{_fmt(headline['contains_rate'])}**")
        if "target_gap" in headline:
            lines.append(
                f"- gap-to-target (target_contains − current): **{_fmt(headline['target_gap'])}**"
            )
        if "benchmark_real_gap_pp" in headline:
            lines.append(
                f"- **benchmark↔real gap: {_fmt(headline['benchmark_real_gap_pp'])} pp** "
                "(clean − real contains)"
            )
        lines.append("")

    # Per-family verdicts.
    lines.append("## Families")
    lines.append("")
    lines.append("| family | status | reasons |")
    lines.append("| --- | --- | --- |")
    for fam in report.get("families", []):
        reasons = "; ".join(fam.get("reasons", [])) or "—"
        lines.append(f"| {fam['name']} | {fam['status']} | {reasons} |")
    lines.append("")

    # Aggregated metrics.
    agg = report.get("aggregated", {})
    if agg:
        lines.append("## Aggregated metrics")
        lines.append("")
        for key in ("contains_rate", "strict_rate", "refusal_rate", "latency_p95_s", "latency_max_s"):
            if key in agg:
                lines.append(f"- {key}: {_fmt(agg.get(key))}")
        lines.append("")

    # Real-usage suite (when present).
    real = report.get("real_usage")
    if real:
        lines.append("## Real-usage suite")
        lines.append("")
        rh = real.get("headline", {})
        lines.append(
            f"- benchmark↔real gap: **{_fmt(rh.get('benchmark_real_gap_pp'))} pp** "
            f"(advisory floor {_fmt(rh.get('max_gap_pp_advisory'))} pp; "
            f"advisory_no_go={rh.get('advisory_no_go')})"
        )
        lines.append(f"- clean contains: {_fmt(real.get('clean_contains_rate'))}")
        lines.append(f"- real contains: {_fmt(real.get('real_contains_rate'))} "
                     f"(source={real.get('real_source')}, n={real.get('real_n')})")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── artifact writing ────────────────────────────────────────────────────────────
def write_artifacts(
    run_dir: Path | str,
    *,
    report: Mapping[str, Any],
    markdown: str,
    predictions: Any,
) -> dict[str, Path]:
    """Write ``report.json`` / ``report.md`` / ``predictions.json`` under ``run_dir``.

    ``run_dir`` is created if missing. Returns the three written paths keyed by
    artifact name. The caller owns choosing ``run_dir`` under the gitignored
    ``RUNS_ROOT`` (or a tmp dir in tests).
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    report_json = run_dir / "report.json"
    report_md = run_dir / "report.md"
    predictions_json = run_dir / "predictions.json"

    report_json.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report_md.write_text(markdown, encoding="utf-8")
    predictions_json.write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"report.json": report_json, "report.md": report_md, "predictions.json": predictions_json}
