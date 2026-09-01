"""Portable test-set loader for the BUFS KPI eval tool.

PURE — stdlib + ``eval_tools.kpi.schema`` only. No ``import config``. No network.
Runs in the default offline ``pytest -m "not integration"`` lane.

Default path
------------
``eval_tools/data/combined88.json`` resolved relative to this file (two levels
up: ``kpi/`` → ``eval_tools/``). Override via, in priority order:

1. Explicit ``path`` argument to :func:`load_testset`.
2. ``OMC_EVAL_TESTSET`` environment variable (absolute or cwd-relative path).
3. Built-in default ``eval_tools/data/combined88.json``.

Returns
-------
:func:`load_testset` → ``(records, testset_sha256)`` where *records* is a list
of :class:`~eval_tools.kpi.schema.Record` dicts normalized via
:func:`~eval_tools.kpi.schema.normalize_record`, and *testset_sha256* is the
hex-string SHA-256 of the raw file bytes.

External Q-A loader
-------------------
:func:`load_qa_dataset` normalizes an externally-supplied question–answer dataset
to canonical records per the WS-0a field-map. Records with ``answer`` but no
``ground_truth`` have ``answer`` re-mapped to ``ground_truth`` (the reference).
Records missing ``ground_truth`` entirely are flagged with ``judge_scored: True``
(caller must route these through the RAGAS judge path).

Scorer hash
-----------
:func:`scorer_hash` returns a stable hex SHA-256 of ``scorer.py``'s source bytes
— used as a regression match-key in the run-context STAMP and baseline comparison
(Gate Semantics §2: a scorer change makes an old baseline incomparable; the hash
change triggers ``REGRESSION: SKIPPED (config/scorer drift)`` rather than a
phantom NO-GO).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from .schema import Record, normalize_record

# ── Path constants ─────────────────────────────────────────────────────────
# eval_tools/kpi/dataset.py → parent = kpi/ → parent.parent = eval_tools/
_EVAL_TOOLS_DIR: Path = Path(__file__).parent.parent
_DEFAULT_TESTSET: Path = _EVAL_TOOLS_DIR / "data" / "combined88.json"
_SCORER_PATH: Path = Path(__file__).parent / "scorer.py"


# ── Path resolution ────────────────────────────────────────────────────────
def _resolve_testset_path(path: str | Path | None) -> Path:
    """Resolve the testset path with priority: explicit > env > default."""
    if path is not None:
        return Path(path)
    env = os.environ.get("OMC_EVAL_TESTSET")
    if env:
        return Path(env)
    return _DEFAULT_TESTSET


# ── Main loader ────────────────────────────────────────────────────────────
def load_testset(
    path: str | Path | None = None,
) -> tuple[list[Record], str]:
    """Load the committed test set.

    Parameters
    ----------
    path:
        Override the testset path. ``None`` → check ``OMC_EVAL_TESTSET`` env
        → fall back to the committed ``eval_tools/data/combined88.json``.

    Returns
    -------
    ``(records, testset_sha256)``
        *records* — list of normalized :class:`~eval_tools.kpi.schema.Record`.
        *testset_sha256* — stable SHA-256 hex of the raw file bytes (64 chars).

    Raises
    ------
    FileNotFoundError
        If the resolved path does not exist.
    ValueError
        If the file is not valid JSON or has no ``results`` list.
    """
    resolved = _resolve_testset_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Test set not found: {resolved}")

    raw = resolved.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Test set is not valid JSON ({resolved}): {exc}") from exc

    # Accept {"meta": ..., "results": [...]} or a bare list.
    if isinstance(data, dict):
        items = data.get("results")
        if items is None:
            raise ValueError(f"Test set JSON has no 'results' key: {resolved}")
        if not isinstance(items, list):
            raise ValueError(f"Test set 'results' is not a list: {resolved}")
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(
            f"Test set JSON must be a dict (with 'results' key) or a bare list: {resolved}"
        )

    records: list[Record] = [normalize_record(r) for r in items]
    return records, sha


# ── External Q-A loader ────────────────────────────────────────────────────
def load_qa_dataset(
    path: str | Path,
    *,
    answerable_default: bool = True,
) -> tuple[list[Record], str]:
    """Load an external question–answer dataset as canonical records.

    Normalizes a user-supplied Q-A file to the canonical record schema per the
    WS-0a field-map (q → ``question``, reference answer → ``ground_truth``).

    Accepted input shapes
    ---------------------
    - ``{"results": [{question, answer|expected_answer|ground_truth, ...}, ...]}``
    - ``[{question, answer|expected_answer|ground_truth, ...}, ...]``

    Normalization rules
    -------------------
    - Record has ``answer`` or ``expected_answer`` but no ``ground_truth`` →
      that field becomes ``ground_truth`` (``answer`` wins if both are present).
      ``answer`` is the external WS-0a field name; ``expected_answer`` is the key
      this repo's own golden sets use (``eval_tools/datasets/*.json``). Contrast
      with dump records, where ``answer`` is the *model's prediction*.
    - ``answerable`` absent → ``answerable_default`` (default ``True``).
    - No ``ground_truth`` after mapping → ``judge_scored: True`` flagged on the
      returned record dict; the caller must route these through the RAGAS judge.

    Parameters
    ----------
    path:
        Absolute or cwd-relative path to the Q-A JSON file.
    answerable_default:
        Value used when a record has no ``answerable`` key.

    Returns
    -------
    ``(records, file_sha256)``
        *records* — list of normalized :class:`~eval_tools.kpi.schema.Record`
        (with optional extra ``judge_scored`` key).
        *file_sha256* — SHA-256 hex of the raw file bytes.

    Raises
    ------
    FileNotFoundError
        If the file is not found.
    ValueError
        If the file is not valid JSON.
    """
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Q-A dataset not found: {resolved}")

    raw = resolved.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Q-A dataset is not valid JSON ({resolved}): {exc}") from exc

    if isinstance(data, dict):
        items: list = data.get("results", [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(
            f"Q-A dataset JSON must be a dict (with 'results' key) or a bare list: {resolved}"
        )

    records: list[Record] = []
    for raw_item in items:
        item: dict = dict(raw_item)  # shallow copy — don't mutate caller's data

        # Re-map the reference answer → ground_truth for Q-A format.
        # Only when ground_truth is absent; otherwise keep the existing ground_truth.
        # ``answer`` is the WS-0a external field; ``expected_answer`` is what this
        # repo's own golden sets use (eval_tools/datasets/*.json). Without the second
        # key every in-repo set fell through to judge_scored and the rule gate scored
        # 0.000 contains on a run whose answers were in fact correct (2026-09-01).
        # ``not item.get(...)``, not ``not in``: a record carrying an empty
        # ground_truth alongside a real reference answer must still be rule-scorable.
        for _ref_key in ("answer", "expected_answer"):
            if not item.get("ground_truth") and item.get(_ref_key):
                item["ground_truth"] = item.pop(_ref_key)
                break

        # Inject answerable default when absent.
        if "answerable" not in item:
            item["answerable"] = answerable_default

        rec = normalize_record(item)

        # Flag records with empty/absent ground_truth for judge-scored evaluation.
        if not rec.get("ground_truth"):
            rec["judge_scored"] = True  # type: ignore[typeddict-unknown-key]

        records.append(rec)
    return records, sha


# ── Scorer hash ────────────────────────────────────────────────────────────
def scorer_hash() -> str:
    """Stable SHA-256 hex of ``scorer.py`` source bytes (regression match-key).

    Used in the run-context STAMP and baseline ``compare`` per Gate Semantics §2:
    a scorer change makes an old baseline incomparable; the hash change triggers
    ``REGRESSION: SKIPPED (config/scorer drift)`` instead of a phantom NO-GO.

    Returns
    -------
    64-char lowercase hex string.
    """
    return hashlib.sha256(_SCORER_PATH.read_bytes()).hexdigest()
