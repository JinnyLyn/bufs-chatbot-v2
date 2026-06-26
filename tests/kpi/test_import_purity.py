"""Import-purity tripwire for eval_tools.kpi pure modules.

Each "pure" module (scorer, gate, dataset, schema, baseline, real_usage) must NOT
import ``config`` or any ``project.*`` module at import time.

Why this matters: ``pyproject.toml`` sets ``pythonpath = ["project"]``, so a bare
``import config`` inside one of these modules *would resolve silently* during the
test run — the trip-wire catches any future regression before it can pollute the
test environment or couple a pure module to the live app's config layer.

Each module is imported in a fresh subprocess with ``project/`` on ``sys.path``
(mirroring pyproject's pythonpath) so results are order-independent and cached
imports from the collection-time process can't mask a regression.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PURE_MODULES = [
    "eval_tools.kpi.scorer",
    "eval_tools.kpi.gate",
    "eval_tools.kpi.dataset",
    "eval_tools.kpi.schema",
    "eval_tools.kpi.baseline",
    "eval_tools.kpi.real_usage",
]

# Probe: add project/ to sys.path (mirrors pyproject.toml pythonpath = ["project"]),
# import the target module, then report any config or project.* modules that leaked in.
_PROBE = (
    "import sys, json;"
    "sys.path.insert(0, {project!r});"
    "import importlib; importlib.import_module({mod!r});"
    "forbidden = sorted(k for k in sys.modules "
    "if k == 'config' or k.startswith('project.'));"
    "print(json.dumps(forbidden))"
)


@pytest.mark.unit
@pytest.mark.parametrize("mod", _PURE_MODULES)
def test_pure_module_does_not_import_config(mod: str) -> None:
    """``import eval_tools.kpi.<mod>`` must not pull in ``config`` or ``project.*``.

    The pyproject.toml ``pythonpath = ["project"]`` makes ``import config`` resolve
    during test runs. This test asserts that none of the pure KPI modules silently
    import config — that coupling would break the offline guarantee and could inject
    environment-specific values into pure unit-tested code.
    """
    project_dir = str(_REPO_ROOT / "project")
    code = _PROBE.format(project=project_dir, mod=mod)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"import {mod} crashed:\n{proc.stderr}"
    )
    forbidden = json.loads(proc.stdout)
    assert forbidden == [], (
        f"importing {mod} pulled in forbidden modules {forbidden!r} — "
        "config / project.* imports belong in CLI entrypoints or lazy-guarded "
        "helpers (profiles._resolve_gen_model), never at module import time"
    )
