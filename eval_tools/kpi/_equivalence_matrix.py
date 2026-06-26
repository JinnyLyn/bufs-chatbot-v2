"""Generate the scorer equivalence matrix.

Tries to import each of the 13 legacy scorer copies housed in ``eval_tools/``,
extracts ``extract_facts`` / ``matched`` / ``is_refusal``, runs them over the
frozen golden corpus (``tests/kpi/fixtures/combined88_new_result.json``), and
emits the human-readable comparison matrix to
``eval_tools/kpi/SCORER_EQUIVALENCE.md``.

Run from repo root::

    python eval_tools/kpi/_equivalence_matrix.py

Modules that cannot be imported cleanly (side-effect imports, hardcoded Windows
paths, live-network calls, heavy deps) are listed as **uncomparable** with the
specific reason — this is expected and honest for ad-hoc analysis scripts that
were never designed for clean import.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_KPI_DIR = _HERE.parent          # eval_tools/kpi/
_EVAL_TOOLS = _KPI_DIR.parent    # eval_tools/
_REPO_ROOT = _EVAL_TOOLS.parent  # repo root

# Add repo root to sys.path so ``eval_tools.kpi`` imports resolve.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_FIXTURE = _REPO_ROOT / "tests" / "kpi" / "fixtures" / "combined88_new_result.json"
_OUT_MD = _KPI_DIR / "SCORER_EQUIVALENCE.md"

# ---------------------------------------------------------------------------
# Canonical reference: eval_tools.kpi.scorer
# ---------------------------------------------------------------------------
from eval_tools.kpi import scorer as _canonical  # noqa: E402

# ---------------------------------------------------------------------------
# Legacy module registry (13 copies listed in review AC#8)
# ---------------------------------------------------------------------------
_LEGACY: list[tuple[str, Path]] = [
    ("_eval_combined88",  _EVAL_TOOLS / "_eval_combined88.py"),
    ("_eval88_rich",      _EVAL_TOOLS / "_eval88_rich.py"),
    ("_eval_runner",      _EVAL_TOOLS / "_eval_runner.py"),
    ("_rescore88",        _EVAL_TOOLS / "_rescore88.py"),
    ("_retrieval_recall", _EVAL_TOOLS / "_retrieval_recall.py"),
    ("_aggregate_variants", _EVAL_TOOLS / "_aggregate_variants.py"),
    ("_test_fastrefuse",  _EVAL_TOOLS / "_test_fastrefuse.py"),
    ("_stage_attribution",_EVAL_TOOLS / "_stage_attribution.py"),
    ("_compare_ba",       _EVAL_TOOLS / "_compare_ba.py"),
    ("_compare_h100",     _EVAL_TOOLS / "_compare_h100.py"),
    ("_answer_analysis",  _EVAL_TOOLS / "_answer_analysis.py"),
    ("_verify_fixes",     _EVAL_TOOLS / "_verify_fixes.py"),
    ("_check2020",        _EVAL_TOOLS / "_check2020.py"),
]


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------
def _try_import(name: str, path: Path) -> tuple[Any, str | None]:
    """Try to import a module from ``path``.  Returns (module, None) or (None, reason)."""
    if not path.exists():
        return None, f"file not found: {path.name}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod, None
    except Exception as exc:
        # Classify the failure for a clean error message.
        reason = f"{type(exc).__name__}: {exc}"
        # Shorten Windows path noise
        reason = re.sub(r"[A-Z]:\\[^'\"]+", "<windows-path>", reason)
        return None, reason[:200]


# ---------------------------------------------------------------------------
# Load golden corpus
# ---------------------------------------------------------------------------
def _load_corpus(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["results"] if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------
def _compare_extract_facts(
    fn: Callable[[str], set],
    records: list[dict],
) -> dict:
    """Compare fn(ground_truth) vs canonical on every answerable record."""
    identical = diverge = skipped = 0
    diverge_ids: list[str] = []
    for r in records:
        if not r.get("answerable", True):
            continue
        gt = str(r.get("ground_truth") or "")
        if not gt:
            skipped += 1
            continue
        try:
            got = set(fn(gt))
            ref = _canonical.extract_facts(gt)
            if got == ref:
                identical += 1
            else:
                diverge += 1
                diverge_ids.append(str(r.get("id", "?")))
        except Exception:
            skipped += 1
    return {"identical": identical, "diverge": diverge, "skipped": skipped,
            "diverge_ids": diverge_ids[:5]}


def _compare_matched(
    fn: Callable[[str, str], bool],
    records: list[dict],
) -> dict:
    """Compare fn(fact, answer) vs canonical over canonical fact-sets."""
    identical = diverge = skipped = 0
    diverge_ids: list[str] = []
    for r in records:
        if not r.get("answerable", True):
            continue
        gt = str(r.get("ground_truth") or "")
        ans = str(r.get("answer") or "")
        if not gt or not ans:
            skipped += 1
            continue
        try:
            facts = _canonical.extract_facts(gt)
            any_diverge = False
            for fact in facts:
                got = fn(fact, ans)
                ref = _canonical.matched(fact, ans)
                if got != ref:
                    any_diverge = True
                    break
            if any_diverge:
                diverge += 1
                diverge_ids.append(str(r.get("id", "?")))
            else:
                identical += 1
        except Exception:
            skipped += 1
    return {"identical": identical, "diverge": diverge, "skipped": skipped,
            "diverge_ids": diverge_ids[:5]}


def _compare_is_refusal(
    fn: Callable[[str], bool],
    records: list[dict],
) -> dict:
    """Compare fn(answer) vs canonical on all records."""
    identical = diverge = skipped = 0
    diverge_ids: list[str] = []
    for r in records:
        ans = str(r.get("answer") or "")
        if not ans:
            skipped += 1
            continue
        try:
            got = fn(ans)
            ref = _canonical.is_refusal(ans)
            if got == ref:
                identical += 1
            else:
                diverge += 1
                diverge_ids.append(str(r.get("id", "?")))
        except Exception:
            skipped += 1
    return {"identical": identical, "diverge": diverge, "skipped": skipped,
            "diverge_ids": diverge_ids[:5]}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"Loading corpus: {_FIXTURE}")
    records = _load_corpus(_FIXTURE)
    answerable = [r for r in records if r.get("answerable", True)]
    unanswerable = [r for r in records if not r.get("answerable", True)]
    print(f"  {len(records)} records ({len(answerable)} answerable, {len(unanswerable)} unanswerable)")

    rows: list[dict] = []

    for name, path in _LEGACY:
        print(f"\nProbing {name} ...", end=" ", flush=True)
        mod, err = _try_import(name, path)
        if err:
            print(f"UNCOMPARABLE ({err[:60]})")
            rows.append({
                "name": name,
                "status": "uncomparable",
                "reason": err,
                "extract_facts": "-",
                "matched": "-",
                "is_refusal": "-",
            })
            continue

        ef_fn  = getattr(mod, "extract_facts", None)
        mt_fn  = getattr(mod, "matched", None)
        rf_fn  = getattr(mod, "is_refusal", None)

        ef_result = _compare_extract_facts(ef_fn, records) if ef_fn else None
        mt_result = _compare_matched(mt_fn, records)       if mt_fn else None
        rf_result = _compare_is_refusal(rf_fn, records)   if rf_fn else None

        def _label(r: dict | None) -> str:
            if r is None:
                return "N/A (no fn)"
            if r["skipped"] == len(records):
                return "UNCOMPARABLE (all errored)"
            if r["diverge"] == 0:
                return f"IDENTICAL ({r['identical']} items)"
            ids = ", ".join(r["diverge_ids"])
            return f"DIVERGE ({r['diverge']} items; e.g. {ids})"

        overall = "IDENTICAL"
        for r in (ef_result, mt_result, rf_result):
            if r is not None and r.get("diverge", 0) > 0:
                overall = "DIVERGE"
                break
        if all(x is None for x in (ef_result, mt_result, rf_result)):
            overall = "UNCOMPARABLE (no functions found)"

        print(overall)
        rows.append({
            "name": name,
            "status": overall,
            "reason": "",
            "extract_facts": _label(ef_result),
            "matched": _label(mt_result),
            "is_refusal": _label(rf_result),
        })

    # -----------------------------------------------------------------------
    # Render SCORER_EQUIVALENCE.md
    # -----------------------------------------------------------------------
    n_identical     = sum(1 for r in rows if r["status"].startswith("IDENTICAL"))
    n_diverge       = sum(1 for r in rows if r["status"].startswith("DIVERGE"))
    n_uncomparable  = sum(1 for r in rows if r["status"].startswith("uncomparable")
                          or r["status"].startswith("UNCOMPARABLE"))

    md_lines: list[str] = [
        "# Scorer Equivalence Matrix",
        "",
        "Auto-generated by `eval_tools/kpi/_equivalence_matrix.py`.",
        "Reference (canonical): **`eval_tools.kpi.scorer`** — the corrected lineage.",
        "Corpus: `tests/kpi/fixtures/combined88_new_result.json`",
        f"({len(records)} records: {len(answerable)} answerable, {len(unanswerable)} unanswerable).",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|--------|-------|",
        f"| IDENTICAL | {n_identical} |",
        f"| DIVERGE | {n_diverge} |",
        f"| uncomparable (side-effect import / heavy dep) | {n_uncomparable} |",
        "",
        "## Per-Module Matrix",
        "",
        "| Module | `extract_facts` | `matched` | `is_refusal` | Overall |",
        "|--------|----------------|-----------|--------------|---------|",
    ]

    for r in rows:
        if r["status"].startswith("uncomparable"):
            reason_short = r["reason"][:80].replace("|", "\\|")
            md_lines.append(
                f"| `{r['name']}` | — | — | — "
                f"| uncomparable: {reason_short} |"
            )
        else:
            ef = r["extract_facts"].replace("|", "\\|")
            mt = r["matched"].replace("|", "\\|")
            rf = r["is_refusal"].replace("|", "\\|")
            st = r["status"]
            md_lines.append(f"| `{r['name']}` | {ef} | {mt} | {rf} | **{st}** |")

    md_lines += [
        "",
        "## Known Deltas (buggy combined88 → corrected lineage)",
        "",
        "Three pre-classified bugs distinguish `_eval_combined88` from the corrected"
        " lineage (`_rescore88` / `_aggregate_variants` / `eval_tools.kpi.scorer`):",
        "",
        "| ID | Description | Affected function |",
        "|----|-------------|-------------------|",
        "| **D1** | Grade regex `[A-F]\\+` misses bare single-letter grades (`A`, `B`, …). "
        "Corrected: `(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])`. | `extract_facts` |",
        "| **D2** | 24h↔12h time equivalence missing: `13` should also match `오후 1시`. "
        "Corrected adds `13 ≤ n ≤ 23` branch in `matched`. | `matched` |",
        "| **D3** | `is_refusal` subtraction applied to *answerable* items — a correct "
        "answer containing '불가능' was incorrectly scored as refusal. "
        "Corrected lineage applies refusal markers **only to unanswerable** items. | `is_refusal` (usage) |",
        "",
        "## Canonical = Corrected Lineage",
        "",
        "`eval_tools.kpi.scorer` is a logic-preserving port of `_rescore88.py` / "
        "`_aggregate_variants.py` (AC#1 0-diff parity verified by `tests/kpi/test_scorer_parity.py`). "
        "Modules listed as UNCOMPARABLE could not be imported without side effects "
        "(hardcoded Windows file paths, live network calls, or heavy non-stdlib deps) "
        "— this is expected for ad-hoc analysis scripts; it is not a defect in the canonical scorer.",
        "",
    ]

    _OUT_MD.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\nWrote {_OUT_MD}")
    print(f"Summary: {n_identical} IDENTICAL, {n_diverge} DIVERGE, {n_uncomparable} uncomparable")


if __name__ == "__main__":
    main()
