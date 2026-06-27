"""Integration tests for the KPI CLI + report (WS-E) — OFFLINE, no network.

The end-to-end ``run --from-predictions`` path is driven against the pinned
h100-fast snapshot (``fixtures/combined88_new_result.json``) using a frozen
fixtures profile (``fixtures/profiles.yaml``), and the resulting ``report.json``
is asserted against a checked-in golden (``fixtures/expected_run_h100.json``).
Everything here runs in the default ``pytest -m "not integration"`` lane:

* ``run`` orchestration (advisory NO-GO → exit 0) + report artifacts + the
  embedded run-context STAMP.
* blocking profile → exit 1; ``--require-retrieval`` → ERROR exit 2.
* ``gate`` re-evaluation of a predictions dump and of a pre-computed metrics file.
* ``baseline-update --set-floors`` rewrites a TEMP-COPY yaml (floors + gating
  flip + FLAG-comment clearing) and REFUSES N=1 / temp≠0 — never touching the
  real ``kpi_profiles.yaml``.

The single live-only assertion (driving the real backend) is marked
``integration`` and deselected by default.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eval_tools.kpi import cli
from eval_tools.kpi.profiles import load_profile

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures"
_DUMP_FIXTURE = _FIXTURES / "combined88_new_result.json"
_PROFILES_FIXTURE = _FIXTURES / "profiles.yaml"
_GOLDEN = _FIXTURES / "expected_run_h100.json"
_REAL_PROFILES_YAML = Path(__file__).resolve().parents[2] / "eval_tools" / "kpi_profiles.yaml"


# ── helpers ───────────────────────────────────────────────────────────────────
def _dump_dir(tmp_path: Path, *, copies: int = 1) -> Path:
    """A predictions dir holding ``copies`` copies of the pinned snapshot (N runs)."""
    d = tmp_path / "dump"
    d.mkdir()
    for i in range(copies):
        shutil.copy(_DUMP_FIXTURE, d / f"run{i + 1}.json")
    return d


def _only_report(runs_root: Path) -> dict:
    """Load the single report.json written under ``runs_root``."""
    reports = list(runs_root.glob("*/report.json"))
    assert len(reports) == 1, f"expected exactly one run dir, found {reports}"
    return json.loads(reports[0].read_text(encoding="utf-8"))


# ── run: end-to-end golden ──────────────────────────────────────────────────────
class TestRunGolden:
    def test_run_from_predictions_matches_golden(self, tmp_path):
        runs_root = tmp_path / "runs"
        exit_code = cli.main([
            "run", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--runs-root", str(runs_root),
            "--baselines-dir", str(tmp_path / "baselines"),  # empty → bootstrap, no baseline
        ])
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert exit_code == golden["exit_code"]

        report = _only_report(runs_root)
        assert report["verdict"] == golden["verdict"]
        assert report["exit_code"] == golden["exit_code"]
        assert report["advisory"] == golden["advisory"]
        assert report["gating"] == golden["gating"]

        statuses = {f["name"]: f["status"] for f in report["families"]}
        for fam, status in golden["family_status"].items():
            assert statuses[fam] == status, f"family {fam}: {statuses[fam]} != {status}"

    def test_report_embeds_full_run_context_stamp(self, tmp_path):
        runs_root = tmp_path / "runs"
        cli.main([
            "run", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--runs-root", str(runs_root),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        ctx = _only_report(runs_root)["run_context"]
        # AC#7 stamp keys must all be present (regression match-key + audit).
        for key in (
            "machine", "backend_url", "gen_url", "gen_model", "num_ctx",
            "fast_refuse", "compress_threshold", "judge", "git_sha",
            "testset_hash", "scorer_hash", "scorer_version", "temp", "seed",
            "N", "profile", "gating", "timestamp",
        ):
            assert key in ctx, f"stamp missing key {key!r}"
        assert ctx["profile"] == "h100-fast"
        assert ctx["gating"] == "advisory"

    def test_run_writes_three_artifacts(self, tmp_path):
        runs_root = tmp_path / "runs"
        cli.main([
            "run", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--runs-root", str(runs_root),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        run_dirs = list(runs_root.glob("*"))
        assert len(run_dirs) == 1
        for name in ("report.json", "report.md", "predictions.json"):
            assert (run_dirs[0] / name).exists(), f"missing artifact {name}"
        md = (run_dirs[0] / "report.md").read_text(encoding="utf-8")
        assert "KPI Gate Report" in md and "NO-GO" in md


# ── exit-code wiring ────────────────────────────────────────────────────────────
class TestExitCodes:
    def test_blocking_profile_fixture_exits_1(self, tmp_path):
        # test-blocking has the same floors but gating: blocking → strict 0.667 < 0.68 → NO-GO exit 1.
        exit_code = cli.main([
            "run", "--profile", "test-blocking",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--runs-root", str(tmp_path / "runs"),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        assert exit_code == 1

    def test_require_retrieval_on_predictions_path_is_error_exit_2(self, tmp_path):
        exit_code = cli.main([
            "run", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--require-retrieval",
            "--runs-root", str(tmp_path / "runs"),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        assert exit_code == 2

    def test_missing_predictions_is_error_exit_2(self, tmp_path):
        exit_code = cli.main([
            "run", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(tmp_path / "does_not_exist"),
            "--runs-root", str(tmp_path / "runs"),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        assert exit_code == 2


# ── gate re-evaluation ───────────────────────────────────────────────────────────
class TestGateSubcommand:
    def test_gate_from_predictions_advisory_exit_0(self, tmp_path):
        exit_code = cli.main([
            "gate", "--profile", "h100-fast",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--from-predictions", str(_dump_dir(tmp_path)),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        assert exit_code == 0  # advisory NO-GO never exits 1

    def test_gate_from_metrics_file(self, tmp_path):
        # A blocking profile + a synthetic sub-floor metrics dump → NO-GO exit 1.
        metrics = [{
            "contains_rate": 0.50, "strict_rate": 0.40, "refusal_rate": 1.0,
            "latency_p95_s": 10.0, "latency_max_s": 12.0,
            "total_count": 89, "excluded_count": 0,
            "measurement_error": None, "ragas": None, "retrieval": None,
        }]
        mfile = tmp_path / "metrics.json"
        mfile.write_text(json.dumps(metrics), encoding="utf-8")
        exit_code = cli.main([
            "gate", "--profile", "test-blocking",
            "--profiles-yaml", str(_PROFILES_FIXTURE),
            "--metrics", str(mfile),
            "--baselines-dir", str(tmp_path / "baselines"),
        ])
        assert exit_code == 1


# ── baseline-update --set-floors (temp-copy yaml; real yaml untouched) ───────────
class TestBaselineUpdate:
    def _synth_dumps(self, tmp_path: Path, n: int) -> Path:
        """N synthetic dumps: all-correct answerable + a correct refusal → contains=strict=1.0."""
        recs = {"results": [
            {"id": "q1", "answerable": True, "ground_truth": "3월 2일",
             "answer": "개강은 3월 2일입니다.", "duration_ms": 1000},
            {"id": "q2", "answerable": True, "ground_truth": "A+",
             "answer": "성적은 A+ 입니다.", "duration_ms": 1100},
            {"id": "u1", "answerable": False, "ground_truth": "",
             "answer": "확인할 수 없습니다.", "duration_ms": 900},
        ]}
        d = tmp_path / "capture"
        d.mkdir()
        for i in range(n):
            (d / f"run{i + 1}.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
        return d

    def test_set_floors_rewrites_tmp_yaml_and_flips_gating(self, tmp_path):
        yaml_copy = tmp_path / "profiles.yaml"
        shutil.copy(_PROFILES_FIXTURE, yaml_copy)

        exit_code = cli.main([
            "baseline-update", "--profile", "h100-fast",
            "--profiles-yaml", str(yaml_copy),
            "--from-predictions", str(self._synth_dumps(tmp_path, n=2)),
            "--baselines-dir", str(tmp_path / "baselines"),
            "--set-floors",
        ])
        assert exit_code == 0

        # The TEMP copy was rewritten: gating flipped, floors set, FLAG comments cleared.
        updated = load_profile("h100-fast", yaml_path=str(yaml_copy))
        assert updated.gating == "blocking"
        # median contains/strict == 1.0 → floor = 1.0 − regression_delta(0.03) = 0.97
        assert updated.thresholds.contains_floor == pytest.approx(0.97)
        assert updated.thresholds.strict_floor == pytest.approx(0.97)
        text = yaml_copy.read_text(encoding="utf-8")
        assert "FLAG" not in text  # all FLAG annotations cleared
        # Sibling profile must be left untouched by the targeted block rewrite.
        assert load_profile("test-blocking", yaml_path=str(yaml_copy)).gating == "blocking"

        # The baseline JSON was written.
        assert (tmp_path / "baselines" / "h100-fast.json").exists()

    def test_set_floors_does_not_mutate_real_kpi_profiles_yaml(self, tmp_path):
        before = _REAL_PROFILES_YAML.read_text(encoding="utf-8")
        yaml_copy = tmp_path / "profiles.yaml"
        shutil.copy(_PROFILES_FIXTURE, yaml_copy)
        cli.main([
            "baseline-update", "--profile", "h100-fast",
            "--profiles-yaml", str(yaml_copy),
            "--from-predictions", str(self._synth_dumps(tmp_path, n=2)),
            "--baselines-dir", str(tmp_path / "baselines"),
            "--set-floors",
        ])
        assert _REAL_PROFILES_YAML.read_text(encoding="utf-8") == before

    def test_refuses_single_dump_n1(self, tmp_path):
        yaml_copy = tmp_path / "profiles.yaml"
        shutil.copy(_PROFILES_FIXTURE, yaml_copy)
        before = yaml_copy.read_text(encoding="utf-8")
        exit_code = cli.main([
            "baseline-update", "--profile", "h100-fast",
            "--profiles-yaml", str(yaml_copy),
            "--from-predictions", str(self._synth_dumps(tmp_path, n=1)),
            "--baselines-dir", str(tmp_path / "baselines"),
            "--set-floors",
        ])
        assert exit_code == 2  # N=1 has no stability evidence
        assert yaml_copy.read_text(encoding="utf-8") == before  # not rewritten

    def test_refuses_unpinned_temperature(self, tmp_path):
        yaml_copy = tmp_path / "profiles.yaml"
        shutil.copy(_PROFILES_FIXTURE, yaml_copy)
        exit_code = cli.main([
            "baseline-update", "--profile", "h100-fast",
            "--profiles-yaml", str(yaml_copy),
            "--from-predictions", str(self._synth_dumps(tmp_path, n=3)),
            "--baselines-dir", str(tmp_path / "baselines"),
            "--temp", "0.7",
            "--set-floors",
        ])
        assert exit_code == 2  # temp != 0 is advisory-only


# ── live-only smoke (deselected by default) ───────────────────────────────────────
@pytest.mark.integration
def test_run_live_backend_smoke(tmp_path):
    """Live: drive the real backend over the test set (needs $BUFS_BACKEND_URL).

    Run: pytest -m integration tests/kpi/test_cli_integration.py::test_run_live_backend_smoke
    """
    import os

    backend = os.environ.get("BUFS_BACKEND_URL")
    if not backend:
        pytest.skip("BUFS_BACKEND_URL not set")
    exit_code = cli.main([
        "run", "--profile", "h100-fast",
        "--backend-url", backend,
        "--runs-root", str(tmp_path / "runs"),
        "--baselines-dir", str(tmp_path / "baselines"),
    ])
    assert exit_code in (0, 1, 2)
    assert list((tmp_path / "runs").glob("*/report.json"))
