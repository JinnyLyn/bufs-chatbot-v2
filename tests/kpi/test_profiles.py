"""Unit tests for eval_tools.kpi.profiles.

Offline, pure, no network. Runs under the default
``pytest -m "not integration"`` lane.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from eval_tools.kpi.dataset import load_testset
from eval_tools.kpi.profiles import (
    SCORER_VERSION,
    JudgeConfig,
    Profile,
    Thresholds,
    build_stamp,
    load_profile,
)

pytestmark = pytest.mark.unit

_YAML = Path(__file__).parent.parent.parent / "eval_tools" / "kpi_profiles.yaml"

# All keys mandated by AC#7. Tests assert none are absent.
_STAMP_REQUIRED_KEYS = frozenset({
    "machine",
    "backend_url",
    "gen_url",
    "gen_model",           # gate match-key; was "model" before coordination with worker-gate
    "num_ctx",
    "fast_refuse",
    "compress_threshold",
    "judge",
    "git_sha",
    "testset_hash",
    "scorer_hash",
    "scorer_version",
    "temp",
    "seed",
    "N",
    "profile",
    "gating",
    "timestamp",
})


# ── load_profile ───────────────────────────────────────────────────────────

class TestLoadProfile:
    def test_h100_fast_loads(self) -> None:
        p = load_profile("h100-fast")
        assert p.name == "h100-fast"

    def test_4090_local_loads(self) -> None:
        p = load_profile("4090-local")
        assert p.name == "4090-local"

    def test_local_cpu_loads(self) -> None:
        p = load_profile("local-cpu")
        assert p.name == "local-cpu"

    # ── gating ──

    def test_h100_fast_gating_advisory(self) -> None:
        """h100-fast is ADVISORY until FLAG floors are measured."""
        p = load_profile("h100-fast")
        assert p.gating == "advisory"
        assert p.is_advisory()

    def test_4090_local_gating_advisory(self) -> None:
        p = load_profile("4090-local")
        assert p.gating == "advisory"

    # ── config knobs ──

    def test_h100_fast_config(self) -> None:
        p = load_profile("h100-fast")
        assert p.fast_refuse is True
        assert p.compress_threshold is None
        assert p.num_ctx == 16384   # deployed H100 .env (MIGRATION_H100.md 3-2)

    def test_4090_local_config(self) -> None:
        p = load_profile("4090-local")
        assert p.fast_refuse is False
        assert p.compress_threshold == 2000
        assert p.num_ctx == 8192

    def test_local_cpu_config(self) -> None:
        p = load_profile("local-cpu")
        assert p.fast_refuse is False
        assert p.num_ctx == 8192

    # ── thresholds ──

    def test_h100_fast_thresholds(self) -> None:
        t = load_profile("h100-fast").thresholds
        assert t.target_contains == pytest.approx(0.90)
        assert t.contains_floor == pytest.approx(0.83)
        assert t.strict_floor == pytest.approx(0.68)
        assert t.refusal_floor == pytest.approx(1.0)
        assert t.flaky_tolerance == pytest.approx(0.13)   # ~1/8: one flip → reported, not NO-GO
        assert t.latency_p95_max_s == pytest.approx(22.0)
        assert t.latency_max_s == pytest.approx(30.0)
        assert t.regression_delta_pp == pytest.approx(3.0)

    def test_4090_local_thresholds(self) -> None:
        t = load_profile("4090-local").thresholds
        assert t.target_contains == pytest.approx(0.92)
        assert t.contains_floor == pytest.approx(0.88)
        assert t.strict_floor == pytest.approx(0.72)
        assert t.refusal_floor == pytest.approx(1.0)
        assert t.flaky_tolerance == pytest.approx(0.13)   # same N=8 brittleness
        assert t.latency_p95_max_s == pytest.approx(25.0)
        assert t.latency_max_s == pytest.approx(35.0)
        assert t.regression_delta_pp == pytest.approx(3.0)

    def test_local_cpu_thresholds_empty(self) -> None:
        """local-cpu Phase-1: intentionally empty thresholds."""
        p = load_profile("local-cpu")
        assert p.thresholds.is_empty()

    # ── URLs / env expansion ──

    def test_env_var_expansion_gen_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """${BUFS_GEN_OLLAMA_URL} is expanded to the env value."""
        monkeypatch.setenv("BUFS_GEN_OLLAMA_URL", "http://h100-host:11434")
        p = load_profile("h100-fast")
        assert p.gen_ollama_url == "http://h100-host:11434"

    def test_env_var_expansion_backend_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BUFS_BACKEND_URL", "http://backend:8000")
        p = load_profile("h100-fast")
        assert p.backend_url == "http://backend:8000"

    def test_missing_env_var_resolves_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env var → empty string (no exception raised)."""
        monkeypatch.delenv("BUFS_BACKEND_URL", raising=False)
        monkeypatch.delenv("BUFS_GEN_OLLAMA_URL", raising=False)
        p = load_profile("h100-fast")
        assert p.backend_url == ""
        assert p.gen_ollama_url == ""

    def test_4090_local_gen_url_from_env_not_hardcoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """4090-local gen_ollama_url resolves from $BUFS_GEN_OLLAMA_URL_4090 — no host committed (S2)."""
        monkeypatch.setenv("BUFS_GEN_OLLAMA_URL_4090", "http://10.0.0.9:11434")
        p = load_profile("4090-local")
        assert p.gen_ollama_url == "http://10.0.0.9:11434"
        # the previously-hardcoded private IP must not be baked into the repo
        assert "100.91.6.58" not in p.gen_ollama_url

    # ── CLI overrides ──

    def test_gen_url_kwarg_overrides_yaml_and_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BUFS_GEN_OLLAMA_URL", "http://env-host:11434")
        p = load_profile("h100-fast", gen_ollama_url="http://cli-host:11434")
        assert p.gen_ollama_url == "http://cli-host:11434"

    def test_backend_url_kwarg_overrides_yaml(self) -> None:
        p = load_profile("h100-fast", backend_url="http://my-backend:8000")
        assert p.backend_url == "http://my-backend:8000"

    # ── Error handling ──

    def test_unknown_profile_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="not found"):
            load_profile("does-not-exist")

    def test_missing_yaml_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_profile("h100-fast", yaml_path=tmp_path / "missing.yaml")

    def test_custom_yaml_path(self, tmp_path: Path) -> None:
        """Explicit yaml_path arg is honoured."""
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            "profiles:\n  my-profile:\n    gating: advisory\n    thresholds: {}\n",
            encoding="utf-8",
        )
        p = load_profile("my-profile", yaml_path=custom)
        assert p.name == "my-profile"
        assert p.gating == "advisory"

    def test_custom_yaml_with_blocking_gating(self, tmp_path: Path) -> None:
        """A profile with gating:blocking loads correctly."""
        custom = tmp_path / "blocking.yaml"
        custom.write_text(
            "profiles:\n  gate-p:\n    gating: blocking\n    thresholds:\n      contains_floor: 0.90\n",
            encoding="utf-8",
        )
        p = load_profile("gate-p", yaml_path=custom)
        assert p.gating == "blocking"
        assert not p.is_advisory()
        assert p.thresholds.contains_floor == pytest.approx(0.90)


# ── build_stamp ────────────────────────────────────────────────────────────

class TestBuildStamp:
    @pytest.fixture()
    def profile_4090(self) -> Profile:
        return load_profile("4090-local")

    @pytest.fixture()
    def profile_h100(self) -> Profile:
        return load_profile("h100-fast")

    @pytest.fixture()
    def testset_hash(self) -> str:
        _, sha = load_testset()
        return sha

    # ── Completeness ──

    def test_all_required_keys_present(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        """AC#7: every mandatory stamp key is present."""
        stamp = build_stamp(profile_4090, testset_hash)
        missing = _STAMP_REQUIRED_KEYS - stamp.keys()
        assert not missing, f"Stamp is missing required keys: {missing}"

    def test_no_extra_undocumented_keys(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        """Stamp doesn't contain random extra keys (predictable shape)."""
        stamp = build_stamp(profile_4090, testset_hash)
        extra = stamp.keys() - _STAMP_REQUIRED_KEYS
        # Extra keys are allowed if explicitly added, but keep the set small.
        # This test catches accidental inflation of the stamp dict.
        assert len(extra) == 0, f"Unexpected stamp keys: {extra}"

    # ── Per-key correctness ──

    def test_profile_key(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["profile"] == "4090-local"

    def test_gating_key(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["gating"] == "advisory"

    def test_testset_hash_passthrough(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        assert build_stamp(profile_4090, testset_hash)["testset_hash"] == testset_hash

    def test_scorer_hash_is_64_hex(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        h = build_stamp(profile_4090, testset_hash)["scorer_hash"]
        assert isinstance(h, str) and len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_scorer_version(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["scorer_version"] == SCORER_VERSION

    def test_fast_refuse_from_profile(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        # 4090-local: fast_refuse=False
        assert build_stamp(profile_4090, testset_hash)["fast_refuse"] is False

    def test_fast_refuse_h100(self, profile_h100: Profile, testset_hash: str) -> None:
        # h100-fast: fast_refuse=True
        assert build_stamp(profile_h100, testset_hash)["fast_refuse"] is True

    def test_num_ctx_from_profile(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        assert build_stamp(profile_4090, testset_hash)["num_ctx"] == 8192

    def test_compress_threshold_4090(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        assert build_stamp(profile_4090, testset_hash)["compress_threshold"] == 2000

    def test_compress_threshold_h100_is_none(
        self, profile_h100: Profile, testset_hash: str
    ) -> None:
        assert build_stamp(profile_h100, testset_hash)["compress_threshold"] is None

    # ── Defaults ──

    def test_temp_default(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["temp"] == 0.0

    def test_seed_default(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["seed"] == 42

    def test_N_default(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash)["N"] == 3

    # ── Overrides ──

    def test_temp_override(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash, temp=0.7)["temp"] == 0.7

    def test_seed_override(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash, seed=123)["seed"] == 123

    def test_N_override(self, profile_4090: Profile, testset_hash: str) -> None:
        assert build_stamp(profile_4090, testset_hash, N=1)["N"] == 1

    def test_gen_model_override(self, profile_4090: Profile, testset_hash: str) -> None:
        stamp = build_stamp(profile_4090, testset_hash, model="qwen3.5:9b")
        assert stamp["gen_model"] == "qwen3.5:9b"

    # ── judge sub-dict ──

    def test_judge_is_dict_with_model_and_url(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        j = build_stamp(profile_4090, testset_hash)["judge"]
        assert isinstance(j, dict)
        assert "model" in j
        assert "url" in j

    # ── timestamp ──

    def test_timestamp_is_iso_string(
        self, profile_4090: Profile, testset_hash: str
    ) -> None:
        ts = build_stamp(profile_4090, testset_hash)["timestamp"]
        assert isinstance(ts, str)
        # Must be parseable as ISO 8601 datetime.
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, "timestamp must be timezone-aware (UTC)"

    def test_git_sha_is_string(self, profile_4090: Profile, testset_hash: str) -> None:
        sha = build_stamp(profile_4090, testset_hash)["git_sha"]
        assert isinstance(sha, str) and len(sha) > 0

    def test_machine_is_string(self, profile_4090: Profile, testset_hash: str) -> None:
        assert isinstance(build_stamp(profile_4090, testset_hash)["machine"], str)

    # ── h100-fast specific ──

    def test_h100_stamp_profile_and_gating(
        self, profile_h100: Profile, testset_hash: str
    ) -> None:
        stamp = build_stamp(profile_h100, testset_hash)
        assert stamp["profile"] == "h100-fast"
        assert stamp["gating"] == "advisory"


# ── Thresholds dataclass ───────────────────────────────────────────────────

class TestThresholds:
    def test_default_is_empty(self) -> None:
        assert Thresholds().is_empty()

    def test_default_flaky_tolerance_is_zero(self) -> None:
        assert Thresholds().flaky_tolerance == 0.0

    def test_nonempty_when_any_floor_set(self) -> None:
        assert not Thresholds(contains_floor=0.88).is_empty()

    def test_from_dict_full(self) -> None:
        t = Thresholds.from_dict({
            "contains_floor": 0.88,
            "strict_floor": 0.72,
            "refusal_floor": 1.0,
            "flaky_tolerance": 0.13,
            "target_contains": 0.92,
            "latency_p95_max_s": 25.0,
            "latency_max_s": 35.0,
            "regression_delta_pp": 3.0,
        })
        assert t.contains_floor == pytest.approx(0.88)
        assert t.strict_floor == pytest.approx(0.72)
        assert t.refusal_floor == pytest.approx(1.0)
        assert t.flaky_tolerance == pytest.approx(0.13)
        assert t.regression_delta_pp == pytest.approx(3.0)

    def test_from_dict_partial(self) -> None:
        t = Thresholds.from_dict({"contains_floor": 0.88})
        assert t.contains_floor == pytest.approx(0.88)
        assert t.strict_floor is None
        assert t.flaky_tolerance == 0.0  # default when absent

    def test_from_empty_dict(self) -> None:
        t = Thresholds.from_dict({})
        assert t.is_empty()
        assert t.flaky_tolerance == 0.0

    def test_flaky_tolerance_from_dict(self) -> None:
        t = Thresholds.from_dict({"flaky_tolerance": 0.13})
        assert t.flaky_tolerance == pytest.approx(0.13)

    def test_local_cpu_flaky_tolerance_is_zero(self) -> None:
        """local-cpu empty thresholds → flaky_tolerance defaults to 0.0."""
        t = load_profile("local-cpu").thresholds
        assert t.flaky_tolerance == 0.0


# ── JudgeConfig dataclass ──────────────────────────────────────────────────

class TestJudgeConfig:
    def test_not_configured_when_empty(self) -> None:
        assert not JudgeConfig().is_configured()

    def test_configured_when_model_set(self) -> None:
        assert JudgeConfig(model="exaone3.5:7.8b").is_configured()

    def test_configured_when_url_set(self) -> None:
        assert JudgeConfig(url="http://judge:11434").is_configured()

    def test_from_dict(self) -> None:
        j = JudgeConfig.from_dict({"model": "exaone3.5:7.8b", "url": "http://x:11434"})
        assert j.model == "exaone3.5:7.8b"
        assert j.url == "http://x:11434"

    def test_from_empty_dict(self) -> None:
        j = JudgeConfig.from_dict({})
        assert j.model == ""
        assert j.url == ""
        assert not j.is_configured()
