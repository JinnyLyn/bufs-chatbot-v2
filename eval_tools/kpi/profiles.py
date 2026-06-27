"""Profile model + loader for ``kpi_profiles.yaml``.

Loads per-profile thresholds, endpoint URLs, and runtime config knobs.
Builds the run-context STAMP dict embedded in every report and baseline.

Import-purity rule (from plan §Guardrails)
------------------------------------------
``scorer.py``, ``gate.py``, and ``dataset.py`` must NOT import ``project.config``.
This module MAY, but only lazily: the ``import config`` call is wrapped in a
``try/except ImportError`` so the module stays importable in the offline pytest
lane (where ``project/`` is not on ``sys.path``). All other modules that need
config-sourced values — backend URL, gen model, etc. — go through env vars or
explicit CLI overrides; the lazy config path is a last-resort read for values
that can only come from the running app's config (e.g. ``LLM_MODEL``).

Environment-variable expansion
-------------------------------
String values containing ``${VAR_NAME}`` placeholders in the YAML are expanded
via ``os.environ`` at profile-load time. Missing vars resolve to ``""`` (no
exception), so the offline unit tests don't need live env vars set.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .dataset import scorer_hash as _scorer_hash

# ── Version ────────────────────────────────────────────────────────────────
# Semantic version for this scorer generation (Phase 1 baseline).
# Bump when the corrected lineage is intentionally changed beyond the three
# pre-classified deltas (D1/D2/D3) documented in scorer.py.
SCORER_VERSION: str = "1.0.0"

# ── Paths ──────────────────────────────────────────────────────────────────
# profiles.py lives at eval_tools/kpi/profiles.py → parent.parent = eval_tools/
_PROFILES_YAML: Path = Path(__file__).parent.parent / "kpi_profiles.yaml"

_ENV_RE: re.Pattern[str] = re.compile(r"\$\{([^}]+)\}")


# ── Env-var expansion ──────────────────────────────────────────────────────
def _expand_env(value: str) -> str:
    """Replace every ``${VAR}`` token with ``os.environ.get(VAR, '')``."""
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _expand_recursive(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders in nested dicts/lists/strs."""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_recursive(v) for v in obj]
    return obj  # bool, int, float, None — pass through unchanged


# ── Data model ─────────────────────────────────────────────────────────────
@dataclass
class Thresholds:
    """Per-profile KPI floors and aspirational targets.

    ``None`` means "not configured" (e.g. ``local-cpu`` in Phase 1).
    """

    target_contains: Optional[float] = None     # aspirational; gap-tracked, never blocks
    contains_floor: Optional[float] = None      # release-blocking accuracy floor
    strict_floor: Optional[float] = None        # release-blocking strict-match floor
    refusal_floor: Optional[float] = None       # routed through flaky_tolerance
    flaky_tolerance: float = 0.0                # fraction: floor − flaky-count/N band
                                                # ~0.13 (1/8) → one unanswerable flip is
                                                # reported, not auto-NO-GO
    latency_p95_max_s: Optional[float] = None
    latency_max_s: Optional[float] = None
    regression_delta_pp: Optional[float] = None  # NOT applied to refusal family

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        return cls(
            target_contains=d.get("target_contains"),
            contains_floor=d.get("contains_floor"),
            strict_floor=d.get("strict_floor"),
            refusal_floor=d.get("refusal_floor"),
            flaky_tolerance=float(d.get("flaky_tolerance") or 0.0),
            latency_p95_max_s=d.get("latency_p95_max_s"),
            latency_max_s=d.get("latency_max_s"),
            regression_delta_pp=d.get("regression_delta_pp"),
        )

    def is_empty(self) -> bool:
        """True when no meaningful threshold is configured (``local-cpu`` Phase-1)."""
        return all(
            v is None
            for v in (
                self.target_contains,
                self.contains_floor,
                self.strict_floor,
                self.refusal_floor,
                self.latency_p95_max_s,
                self.latency_max_s,
            )
        )


@dataclass
class JudgeConfig:
    """LLM judge endpoint for RAGAS opt-in scoring."""

    model: str = ""
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "JudgeConfig":
        return cls(model=d.get("model", ""), url=d.get("url", ""))

    def is_configured(self) -> bool:
        """True when at least model or URL is set (judge endpoint usable)."""
        return bool(self.model or self.url)


@dataclass
class Profile:
    """Resolved runtime profile (env vars expanded, CLI overrides applied)."""

    name: str
    gating: str                              # "advisory" | "blocking"
    backend_url: str = ""
    gen_ollama_url: str = ""
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    fast_refuse: bool = False
    compress_threshold: Optional[int] = None
    num_ctx: int = 8192
    thresholds: Thresholds = field(default_factory=Thresholds)
    fail_policy: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    def is_advisory(self) -> bool:
        """True when this profile runs in advisory mode (never exits 1)."""
        return self.gating == "advisory"


# ── Loader ─────────────────────────────────────────────────────────────────
def load_profile(
    name: str,
    yaml_path: str | Path | None = None,
    *,
    backend_url: str | None = None,
    gen_ollama_url: str | None = None,
) -> Profile:
    """Load and resolve a named profile from ``kpi_profiles.yaml``.

    Resolution order for URL fields:
    1. Explicit kwarg (``backend_url`` / ``gen_ollama_url``) — highest priority.
    2. ``${ENV_VAR}`` expansion of the YAML value.
    3. Literal YAML value (e.g. ``http://100.91.6.58:11434`` for 4090-local).

    Parameters
    ----------
    name:
        Profile key in the YAML (e.g. ``"h100-fast"``).
    yaml_path:
        Override the default ``eval_tools/kpi_profiles.yaml`` path.
    backend_url:
        CLI / caller override for the backend URL.
    gen_ollama_url:
        CLI / caller override for the gen Ollama URL.

    Raises
    ------
    FileNotFoundError
        If the YAML file is not found.
    KeyError
        If the named profile does not exist in the YAML.
    ValueError
        If the YAML is not parseable.
    """
    resolved_yaml = Path(yaml_path) if yaml_path is not None else _PROFILES_YAML
    if not resolved_yaml.exists():
        raise FileNotFoundError(f"kpi_profiles.yaml not found: {resolved_yaml}")

    with resolved_yaml.open(encoding="utf-8") as fh:
        try:
            raw_all: dict = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Could not parse YAML ({resolved_yaml}): {exc}") from exc

    profiles_block: dict = raw_all.get("profiles", raw_all)
    if name not in profiles_block:
        available = sorted(profiles_block.keys())
        raise KeyError(
            f"Profile {name!r} not found in {resolved_yaml}. Available: {available}"
        )

    # Expand env-var placeholders before extracting fields.
    p_raw: dict = _expand_recursive(profiles_block[name]) or {}

    cfg: dict = p_raw.get("config") or {}
    thr_raw: dict = p_raw.get("thresholds") or {}
    judge_raw: dict = p_raw.get("judge") or {}

    return Profile(
        name=name,
        gating=p_raw.get("gating", "advisory"),
        backend_url=backend_url if backend_url is not None else p_raw.get("backend_url", ""),
        gen_ollama_url=(
            gen_ollama_url if gen_ollama_url is not None else p_raw.get("gen_ollama_url", "")
        ),
        judge=JudgeConfig.from_dict(judge_raw),
        fast_refuse=bool(cfg.get("fast_refuse", False)),
        compress_threshold=cfg.get("compress_threshold"),  # None stays None
        num_ctx=int(cfg.get("num_ctx", 8192)),
        thresholds=Thresholds.from_dict(thr_raw),
        fail_policy=p_raw.get("fail_policy") or {},
        raw=p_raw,
    )


# ── Runtime helpers ─────────────────────────────────────────────────────────
def _git_sha() -> str:
    """Current HEAD's short SHA, or ``"unknown"`` if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _machine_label() -> str:
    """Best-effort hostname for the run-context stamp."""
    try:
        return socket.gethostname()
    except Exception:
        return platform.node() or "unknown"


def _resolve_gen_model(override: str | None) -> str:
    """Resolve the gen model name: explicit override > env > lazy config read."""
    if override is not None:
        return override
    env_model = os.environ.get("BUFS_GEN_MODEL", "")
    if env_model:
        return env_model
    # Lazy import: project/config.py is on sys.path only in the full runtime env.
    # Guarded so the offline pytest lane can import this module without project/.
    try:
        import config as _cfg  # type: ignore[import-not-found]
        return str(
            getattr(_cfg, "LLM_MODEL", None)
            or getattr(_cfg, "GEN_MODEL", None)
            or ""
        )
    except ImportError:
        return ""


# ── STAMP builder ──────────────────────────────────────────────────────────
def build_stamp(
    profile: Profile,
    testset_hash: str,
    *,
    temp: float = 0.0,
    seed: int = 42,
    N: int = 3,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the run-context STAMP dict for a report / gate run.

    All keys required by AC#7 are present. The stamp is embedded verbatim in
    the machine-JSON report and used as the regression match-key in
    ``baseline.py`` (Gate Semantics §2).

    Parameters
    ----------
    profile:
        Resolved :class:`Profile` (from :func:`load_profile`).
    testset_hash:
        SHA-256 hex from :func:`~eval_tools.kpi.dataset.load_testset`.
    temp:
        Generation temperature (``0.0`` for gating runs).
    seed:
        Generation seed for reproducibility.
    N:
        Number of run repetitions.
    model:
        Gen model name override. ``None`` → env var / lazy config read.

    Returns
    -------
    Dict with all AC#7 stamp keys:
    ``machine``, ``backend_url``, ``gen_url``, ``gen_model``, ``num_ctx``,
    ``fast_refuse``, ``compress_threshold``, ``judge``, ``git_sha``,
    ``testset_hash``, ``scorer_hash``, ``scorer_version``, ``temp``,
    ``seed``, ``N``, ``profile``, ``gating``, ``timestamp``.
    """
    return {
        "machine": _machine_label(),
        "backend_url": profile.backend_url,
        "gen_url": profile.gen_ollama_url,
        "gen_model": _resolve_gen_model(model),
        "num_ctx": profile.num_ctx,
        "fast_refuse": profile.fast_refuse,
        "compress_threshold": profile.compress_threshold,
        "judge": {
            "model": profile.judge.model,
            "url": profile.judge.url,
        },
        "git_sha": _git_sha(),
        "testset_hash": testset_hash,
        "scorer_hash": _scorer_hash(),
        "scorer_version": SCORER_VERSION,
        "temp": temp,
        "seed": seed,
        "N": N,
        "profile": profile.name,
        "gating": profile.gating,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
