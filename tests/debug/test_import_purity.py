"""Import-purity tripwire for the debug/ package.

Importing any debug module must NEVER mutate os.environ. The package loads
project/.env only inside CLI entrypoints (main()) or lazy bootstraps
(langfuse_client.ensure_env(), repro._bootstrap_env()).

Why this matters: pytest imports test modules (and their imports) at collection
time. A module-level load_dotenv() injects real .env values into the whole test
process and poisons every env-sensitive test downstream — this happened once
(53 unit tests failed on any machine with project/.env present, i.e. the
production box). Each module is imported in a fresh subprocess so results are
order-independent and cached imports can't hide a regression.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DEBUG_MODULES = [
    "logs",
    "_query",
    "langfuse_client",
    "analyze",
    "pipeline",
    "session",
    "status",
    "repro",
    "__main__",  # launcher: import must not run the menu, mutate env, or touch streams
]

_PROBE = (
    "import os, sys, json;"
    "sys.path.insert(0, {root!r});"
    "before = dict(os.environ);"
    "import importlib; importlib.import_module('debug.{mod}');"
    "changed = sorted(k for k, v in os.environ.items() if before.get(k) != v);"
    "removed = sorted(k for k in before if k not in os.environ);"
    "print(json.dumps({{'changed': changed, 'removed': removed}}))"
)


@pytest.mark.parametrize("mod", _DEBUG_MODULES)
def test_import_does_not_mutate_environ(mod: str) -> None:
    """`import debug.<mod>` in a clean interpreter leaves os.environ untouched."""
    code = _PROBE.format(root=str(_REPO_ROOT), mod=mod)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, f"import debug.{mod} crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout)
    assert result == {"changed": [], "removed": []}, (
        f"importing debug.{mod} mutated os.environ: {result} — "
        "load_dotenv/env writes belong in main()/ensure_env(), never at import"
    )


_STREAM_PROBE = (
    "import sys, json;"
    "sys.path.insert(0, {root!r});"
    "before = (sys.stdout.encoding, sys.stdout.errors, sys.stderr.encoding, sys.stderr.errors);"
    "import importlib; importlib.import_module('debug.{mod}');"
    "after = (sys.stdout.encoding, sys.stdout.errors, sys.stderr.encoding, sys.stderr.errors);"
    "print(json.dumps({{'before': before, 'after': after}}))"
)


@pytest.mark.parametrize("mod", _DEBUG_MODULES)
def test_import_does_not_reconfigure_streams(mod: str) -> None:
    """`import debug.<mod>` must not call sys.stdout/stderr.reconfigure().

    A module-level reconfigure() mutates the process's stream encoding for any
    importer (pytest, another tool). UTF-8 reconfiguration belongs in main(),
    not at import. (CodeRabbit flagged this in pipeline.py/session.py; the same
    pattern existed in analyze.py/status.py — this tripwire covers all of them.)
    """
    code = _STREAM_PROBE.format(root=str(_REPO_ROOT), mod=mod)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, f"import debug.{mod} crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout)
    assert result["before"] == result["after"], (
        f"importing debug.{mod} reconfigured stdout/stderr: "
        f"{result['before']} → {result['after']} — "
        "sys.stdout.reconfigure() belongs in main(), never at import"
    )
