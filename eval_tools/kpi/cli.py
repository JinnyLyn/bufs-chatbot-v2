"""``python -m eval_tools.kpi`` — orchestrate runners → gate → report (WS-E).

Subcommands
-----------
``run``              Score predictions (or a live run), gate, and write a report.
``gate``             Re-evaluate an existing predictions / metrics dump (no write).
``baseline-update``  Capture an N-dump baseline; ``--set-floors`` seeds floors +
                     flips the profile ``gating: advisory → blocking``.

Process exit codes (the gate's contract — never drifts)::

    0  GO        (or advisory NO-GO — advisory never exits 1)
    1  NO-GO     (blocking floor / regression failure)
    2  ERROR     (could not measure: backend down, error-budget overrun, bad input)

Offline guarantee
-----------------
The ``--from-predictions`` path imports NO live deps (no backend, Ollama, or
Qdrant). RAGAS without a configured judge returns a SKIPPED sentinel; retrieval
on a dump path is SKIPPED by construction. This keeps the integration test in
the default ``pytest -m "not integration"`` lane. Only the live ``run`` path
(no ``--from-predictions``) touches the backend, and it imports lazily.

No hardcoded hosts: every URL comes from the resolved profile or a CLI override.
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from . import baseline as _baseline
from . import report as _report
from .dataset import load_qa_dataset, load_testset
from .gate import EXIT_ERROR, aggregate_runs, evaluate_gate
from .profiles import Profile, build_stamp, load_profile
from .runners import build_run_metrics
from .runners import latency as _latency
from .runners import ragas as _ragas
from .runners import rulebased as _rulebased


# ═══════════════════════════════════════════════════════════════════════════
# prediction-dump loading (the --from-predictions path)
# ═══════════════════════════════════════════════════════════════════════════
def _expand_prediction_paths(spec: str) -> list[Path]:
    """Resolve a ``--from-predictions`` spec to a sorted list of dump files.

    Accepts a directory (all ``*.json`` within), a glob pattern, or a single
    file. Each resolved file is one run/dump (N dumps → N runs).
    """
    path = Path(spec)
    if path.is_dir():
        files = sorted(path.glob("*.json"))
    elif any(ch in spec for ch in "*?[]"):
        files = sorted(Path(p) for p in _glob.glob(spec))
    elif path.is_file():
        files = [path]
    else:
        files = []
    return [f for f in files if f.is_file()]


def _load_dump_records(path: Path) -> list[dict]:
    """Load one prediction dump's raw records.

    Accepts every shape a dump can take, including the one a live ``run`` writes:
      * ``{"results": [...]}``           — legacy / hand-authored fixture
      * ``{"records": [...]}``           — single normalized dump
      * ``[<record>, ...]``              — bare list of records
      * ``[{"source": ..., "records": [...]}, ...]`` — what ``cli`` persists to
        ``predictions.json`` (a list of per-source wrappers). The records are
        flattened across wrappers so a real run's own output round-trips.

    Returns RAW records (not normalized): the rulebased runner normalizes
    internally, and the latency runner needs the raw ``duration_ms`` / ``timing``
    fields that normalization would drop.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("records", "results"):
            items = data.get(key)
            if isinstance(items, list):
                return items
        raise ValueError(f"prediction dump dict has no 'records'/'results' list: {path}")
    if isinstance(data, list):
        # Distinguish a bare record list from a list of {source, records} wrappers:
        # a wrapper has a 'records' list and is NOT itself a scorable record.
        if data and all(
            isinstance(el, dict) and isinstance(el.get("records"), list) and "answer" not in el
            for el in data
        ):
            out: list[dict] = []
            for wrapper in data:
                out.extend(wrapper["records"])
            return out
        return data
    raise ValueError(f"prediction dump must be a dict (with 'records'/'results') or a list: {path}")


# ═══════════════════════════════════════════════════════════════════════════
# scoring → per-run metric dicts
# ═══════════════════════════════════════════════════════════════════════════
def _score_dump(records: Sequence[dict], profile: Profile, *, with_ragas: bool) -> dict:
    """Score one dump's records into a per-run metric dict (gate contract).

    RAGAS is invoked only when ``with_ragas`` is set; offline (no judge
    configured in the profile) it returns a SKIPPED sentinel — no live deps.
    Retrieval is always SKIPPED on the dump path (``retrieval=None``); the gate
    converts that to ERROR only under ``--require-retrieval``.
    """
    score = _rulebased.run(records)
    latency = _latency.run(records)

    ragas_result = None
    if with_ragas:
        ragas_result = _ragas.run(
            list(records),
            judge_url=profile.judge.url or None,
            judge_model=profile.judge.model or None,
        )

    return build_run_metrics(
        score=score,
        latency=latency,
        ragas=ragas_result,
        retrieval=None,
        total_count=len(records),
        excluded_count=0,
        measurement_error=None,
    )


def _runs_from_predictions(spec: str, profile: Profile, *, with_ragas: bool) -> tuple[list[dict], list[str], list[list[dict]]]:
    """Score every dump under ``spec`` → (per-run metric dicts, source names, raw dumps)."""
    paths = _expand_prediction_paths(spec)
    if not paths:
        raise FileNotFoundError(f"no prediction dumps matched: {spec}")
    runs: list[dict] = []
    sources: list[str] = []
    raw_dumps: list[list[dict]] = []
    for path in paths:
        records = _load_dump_records(path)
        runs.append(_score_dump(records, profile, with_ragas=with_ragas))
        sources.append(path.name)
        raw_dumps.append(records)
    return runs, sources, raw_dumps


def _resolve_testset_hash(testset: Optional[str], qa_format: bool) -> str:
    """Best-effort testset SHA for the run-context match-key (``""`` on failure)."""
    try:
        if qa_format and testset:
            return load_qa_dataset(testset)[1]
        return load_testset(testset)[1]
    except (FileNotFoundError, ValueError):
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# live run path (integration-only; lazy backend import)
# ═══════════════════════════════════════════════════════════════════════════
def _runs_from_backend(profile: Profile, *, testset: Optional[str], qa_format: bool) -> tuple[list[dict], list[list[dict]]]:
    """Drive the live backend over the test set → a single per-run metric dict.

    LIVE / integration-only. Imports the backend client lazily so the offline
    ``--from-predictions`` path never pulls in ``requests``.
    """
    if not profile.backend_url:
        raise ValueError(
            "live run requires a backend URL (profile.backend_url / --backend-url); "
            "use --from-predictions for the offline path"
        )
    from .runners import backend_client  # lazy — live path only

    records, _ = (load_qa_dataset(testset) if (qa_format and testset) else load_testset(testset))
    answered: list[dict] = []
    excluded = 0
    measurement_error: Optional[str] = None
    for rec in records:
        question = str(rec.get("question") or "")
        try:
            event = backend_client.ask(profile.backend_url, question)
        except Exception as exc:  # backend down / SSE error → measurement ERROR
            measurement_error = f"backend error on {rec.get('id')!r}: {exc}"
            excluded += 1
            continue
        answered.append({**rec, "answer": event.answer, "duration_ms": event.duration_ms,
                         "timing": event.timing})

    metrics = build_run_metrics(
        score=_rulebased.run(answered),
        latency=_latency.run(answered),
        total_count=len(records),
        excluded_count=excluded,
        measurement_error=measurement_error,
    )
    return [metrics], [answered]


# ═══════════════════════════════════════════════════════════════════════════
# real-usage headline (optional, offline-capable via --real-from-predictions)
# ═══════════════════════════════════════════════════════════════════════════
def _build_real_usage(clean_dumps: Sequence[Sequence[dict]], real_spec: str):
    """Build a RealUsageFamily from the clean dumps + a real-usage predictions spec.

    Uses the FIRST clean dump as the clean reference and the FIRST real dump as
    the real-usage set (both scored offline against preserved ground_truth).
    """
    from .real_usage import from_scores  # local import keeps top-level surface lean

    real_paths = _expand_prediction_paths(real_spec)
    if not real_paths or not clean_dumps:
        return None
    clean_score = _rulebased.run(clean_dumps[0])
    real_score = _rulebased.run(_load_dump_records(real_paths[0]))
    return from_scores(clean_score, real_score, real_source="perturb")


# ═══════════════════════════════════════════════════════════════════════════
# subcommand: run
# ═══════════════════════════════════════════════════════════════════════════
def _cmd_run(args: argparse.Namespace) -> int:
    profile = load_profile(
        args.profile,
        yaml_path=args.profiles_yaml,
        backend_url=args.backend_url,
    )
    thresholds = dict(profile.raw.get("thresholds") or {})

    if args.from_predictions:
        runs, sources, raw_dumps = _runs_from_predictions(
            args.from_predictions, profile, with_ragas=args.with_ragas
        )
    else:
        runs, raw_dumps = _runs_from_backend(profile, testset=args.testset, qa_format=args.format == "qa")
        sources = ["live-backend"]

    testset_hash = _resolve_testset_hash(args.testset, args.format == "qa")
    stamp = build_stamp(profile, testset_hash, temp=0.0, seed=args.seed, N=len(runs))
    baseline = _baseline.load_baseline(profile.name, baselines_dir=args.baselines_dir)

    result = evaluate_gate(
        runs,
        thresholds,
        gating=profile.gating,
        run_context=stamp,
        baseline=baseline,
        require_ragas=args.require_ragas,
        require_retrieval=args.require_retrieval,
    )

    real_usage = None
    if args.real_from_predictions:
        real_usage = _build_real_usage(raw_dumps, args.real_from_predictions)

    report_dict = _report.build_report(result, stamp, real_usage=real_usage, sources=sources)
    markdown = _report.render_markdown(report_dict)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs_root = Path(args.runs_root) if args.runs_root else _report.RUNS_ROOT
    run_dir = runs_root / _report.run_dir_name(ts, profile.name, str(stamp.get("git_sha", "nogit")))
    predictions = [{"source": s, "records": d} for s, d in zip(sources, raw_dumps)]
    paths = _report.write_artifacts(run_dir, report=report_dict, markdown=markdown, predictions=predictions)

    print(result.summary())
    print(f"\nreport: {paths['report.json']}")
    return result.exit_code


# ═══════════════════════════════════════════════════════════════════════════
# subcommand: gate (re-evaluate an existing dump, no artifact write)
# ═══════════════════════════════════════════════════════════════════════════
def _cmd_gate(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile, yaml_path=args.profiles_yaml)
    thresholds = dict(profile.raw.get("thresholds") or {})

    if args.metrics:
        loaded = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
        # Accept a bare list of per-run metric dicts, or a report.json with "runs".
        runs = loaded if isinstance(loaded, list) else loaded.get("runs") or []
        if not runs:
            print("ERROR: --metrics file has no per-run metric dicts", file=sys.stderr)
            return EXIT_ERROR
    elif args.from_predictions:
        runs, _sources, _raw = _runs_from_predictions(
            args.from_predictions, profile, with_ragas=args.with_ragas
        )
    else:
        print("ERROR: gate needs --from-predictions or --metrics", file=sys.stderr)
        return EXIT_ERROR

    testset_hash = _resolve_testset_hash(args.testset, args.format == "qa")
    stamp = build_stamp(profile, testset_hash, temp=0.0, seed=args.seed, N=len(runs))
    baseline = _baseline.load_baseline(profile.name, baselines_dir=args.baselines_dir)

    result = evaluate_gate(
        runs,
        thresholds,
        gating=profile.gating,
        run_context=stamp,
        baseline=baseline,
        require_ragas=args.require_ragas,
        require_retrieval=args.require_retrieval,
    )
    print(result.summary())
    return result.exit_code


# ═══════════════════════════════════════════════════════════════════════════
# subcommand: baseline-update
# ═══════════════════════════════════════════════════════════════════════════
def _rewrite_profile_yaml(text: str, profile: str, floors: dict[str, float]) -> str:
    """Within ``profile``'s block only: set floors, flip gating→blocking, clear FLAG comments.

    Line-based (comment-preserving) edit — ``yaml.safe_load``/``dump`` would
    destroy the file's comments and layout. Only the named profile's block is
    touched, so sibling profiles (e.g. ``4090-local``, also advisory) are left
    untouched.
    """
    lines = text.splitlines(keepends=True)
    header_re = re.compile(rf"^(\s*){re.escape(profile)}:\s*(#.*)?$")

    start: Optional[int] = None
    header_indent = 0
    for i, line in enumerate(lines):
        m = header_re.match(line)
        if m:
            start, header_indent = i, len(m.group(1))
            break
    if start is None:
        raise KeyError(f"profile {profile!r} not found in kpi_profiles.yaml")

    # Block ends at the next non-blank, non-comment line indented <= the header.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(lines[j]) - len(lines[j].lstrip()) <= header_indent:
            end = j
            break

    gating_re = re.compile(r"^(\s*)gating:\s*\S+.*$")
    contains_re = re.compile(r"^(\s*)contains_floor:\s.*$")
    strict_re = re.compile(r"^(\s*)strict_floor:\s.*$")

    out: list[str] = lines[:start]
    # When a floor line is rewritten, its FLAG annotation may continue onto the
    # following deeply-indented standalone comment lines (the "# NB: …" block);
    # consume those continuations so no stale advisory note is left dangling.
    skip_continuation = False
    for k in range(start, end):
        line = lines[k]
        nl = "\n" if line.endswith("\n") else ""
        stripped = line.strip()

        if stripped.startswith("#"):
            # Drop inline-FLAG-marked lines and continuation comments under floors.
            if "FLAG" in stripped or skip_continuation:
                continue
            out.append(line)
            continue
        skip_continuation = False  # any real key/value ends a continuation run

        m = gating_re.match(line)
        if m:
            out.append(f"{m.group(1)}gating: blocking{nl}")
            continue
        m = contains_re.match(line)
        if m and "contains_floor" in floors:
            out.append(f"{m.group(1)}contains_floor: {floors['contains_floor']}{nl}")
            skip_continuation = True
            continue
        m = strict_re.match(line)
        if m and "strict_floor" in floors:
            out.append(f"{m.group(1)}strict_floor: {floors['strict_floor']}{nl}")
            skip_continuation = True
            continue
        out.append(line)

    out.extend(lines[end:])
    return "".join(out)


def _cmd_baseline_update(args: argparse.Namespace) -> int:
    if not args.from_predictions:
        print("ERROR: baseline-update needs --from-predictions (the N-dump capture)", file=sys.stderr)
        return EXIT_ERROR

    profile = load_profile(args.profile, yaml_path=args.profiles_yaml)
    runs, _sources, _raw = _runs_from_predictions(
        args.from_predictions, profile, with_ragas=False
    )
    n_runs = len(runs)

    eligibility = _baseline.can_update_baseline(n_runs, args.temp)
    if not eligibility.allowed:
        print(f"REFUSED: {eligibility.reason}", file=sys.stderr)
        return EXIT_ERROR

    aggregated = aggregate_runs(runs)
    testset_hash = _resolve_testset_hash(args.testset, args.format == "qa")
    stamp = build_stamp(profile, testset_hash, temp=args.temp, seed=args.seed, N=n_runs)

    baseline = _baseline.make_baseline(
        profile.name, stamp, aggregated, n_runs=n_runs, temperature=args.temp, seed=args.seed
    )
    baseline_path = _baseline.save_baseline(profile.name, baseline, baselines_dir=args.baselines_dir)
    print(f"baseline written: {baseline_path}")

    if args.set_floors:
        regression_delta_pp = float((profile.raw.get("thresholds") or {}).get("regression_delta_pp") or 0.0)
        floors = _baseline.compute_floors(aggregated, regression_delta_pp)
        if not floors:
            print("REFUSED: no accuracy metrics to derive floors from", file=sys.stderr)
            return EXIT_ERROR
        yaml_path = Path(args.profiles_yaml) if args.profiles_yaml else _default_profiles_yaml()
        new_text = _rewrite_profile_yaml(yaml_path.read_text(encoding="utf-8"), profile.name, floors)
        yaml_path.write_text(new_text, encoding="utf-8")
        print(f"floors set in {yaml_path}: {floors}; gating flipped advisory→blocking")

    return 0


def _default_profiles_yaml() -> Path:
    # Mirror profiles._PROFILES_YAML without importing a private name.
    return Path(__file__).resolve().parent.parent / "kpi_profiles.yaml"


# ═══════════════════════════════════════════════════════════════════════════
# argument parsing
# ═══════════════════════════════════════════════════════════════════════════
def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--profile", default="h100-fast", help="profile key in kpi_profiles.yaml")
    sub.add_argument("--profiles-yaml", default=None, help="override kpi_profiles.yaml path (testing)")
    sub.add_argument("--baselines-dir", default=None, help="override baselines dir (testing)")
    sub.add_argument("--testset", default=None, help="override testset path for the run-context hash")
    sub.add_argument("--format", choices=("default", "qa"), default="default", help="testset format")
    sub.add_argument("--seed", type=int, default=42, help="generation seed recorded in the stamp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval_tools.kpi",
        description="BUFS KPI gate — orchestrate runners → gate → report.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="score + gate + write a report")
    _add_common(p_run)
    p_run.add_argument("--from-predictions", default=None,
                       help="dir/glob/file of prediction dumps (offline path; N dumps = N runs)")
    p_run.add_argument("--real-from-predictions", default=None,
                       help="dir/glob/file of real-usage prediction dumps (headline gap KPI)")
    p_run.add_argument("--with-ragas", action="store_true", help="run RAGAS (SKIPPED offline without a judge)")
    p_run.add_argument("--require-ragas", action="store_true", help="ERROR (exit 2) if RAGAS not measured")
    p_run.add_argument("--require-retrieval", action="store_true", help="ERROR (exit 2) if retrieval not measured")
    p_run.add_argument("--backend-url", default=None, help="live backend URL override (live run path)")
    p_run.add_argument("--runs-root", default=None, help="override the runs/ artifact root (testing)")
    p_run.set_defaults(func=_cmd_run)

    # gate
    p_gate = subparsers.add_parser("gate", help="re-evaluate an existing predictions/metrics dump")
    _add_common(p_gate)
    p_gate.add_argument("--from-predictions", default=None, help="dir/glob/file of prediction dumps")
    p_gate.add_argument("--metrics", default=None, help="JSON file: list of per-run metric dicts (or a report.json)")
    p_gate.add_argument("--with-ragas", action="store_true", help="run RAGAS (SKIPPED offline without a judge)")
    p_gate.add_argument("--require-ragas", action="store_true", help="ERROR (exit 2) if RAGAS not measured")
    p_gate.add_argument("--require-retrieval", action="store_true", help="ERROR (exit 2) if retrieval not measured")
    p_gate.set_defaults(func=_cmd_gate)

    # baseline-update
    p_bu = subparsers.add_parser("baseline-update", help="capture an N-dump baseline; optionally seed floors")
    _add_common(p_bu)
    p_bu.add_argument("--from-predictions", default=None, help="dir/glob/file of the N-dump capture")
    p_bu.add_argument("--set-floors", action="store_true",
                      help="compute floors + write them into kpi_profiles.yaml + flip gating→blocking")
    p_bu.add_argument("--temp", type=float, default=0.0, help="generation temperature of the capture (must be 0 to update)")
    p_bu.set_defaults(func=_cmd_baseline_update)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args and dispatch. Returns the process exit code (0/1/2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())
