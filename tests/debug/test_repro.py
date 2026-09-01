"""Offline unit tests for debug.repro — search-k resolution + argparse + purity.

Fully offline: no torch, no qdrant, no Langfuse, no network. These tests cover
the pure ``_resolve_search_k`` helper (BUG #2: production retrieval k is the
LLM-chosen ``limit``, not ``config.MAX_TOOL_CALLS``) and the ``search`` subparser
``--k`` flag, plus an import-purity guard that the Langfuse query layer stays a
lazy in-function import.

The trace_detail fixtures mirror the real Langfuse ``/api/public/traces/{id}``
shape (observations list; each search tool call is a ``search_child_chunks``
observation whose ``input`` dict carries the LLM-chosen ``limit``) — see
debug/_query.get_trace_detail and rag_agent/tools._search_child_chunks.
"""

from __future__ import annotations

import inspect
import re

import pytest

from debug.repro import _build_parser, _bootstrap_env, _resolve_search_k


@pytest.fixture
def parser():
    """A built `search`-capable parser.

    `_build_parser` interpolates `config.SEARCH_SCORE_THRESHOLD` into help text,
    so config must be bootstrapped first — exactly as `main()` does. That loads
    project/.env into os.environ, so the fixture restores the env (and reloads
    config) afterwards: on a box whose .env flips retrieval levers
    (SEMESTER_FILTER_ENABLED=true in production), leaking it made every
    later-collected lever-OFF test in the suite run lever-ON. Import-purity is
    verified in isolated subprocesses (tests/debug/test_import_purity.py) and is
    unaffected.
    """
    import importlib
    import os
    import sys

    before = dict(os.environ)
    _bootstrap_env()
    yield _build_parser()
    os.environ.clear()
    os.environ.update(before)
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: realistic trace_detail dicts (Langfuse REST trace-detail shape)
# ─────────────────────────────────────────────────────────────────────────────

def _search_obs(obs_id: str, limit) -> dict:
    """One ``search_child_chunks`` tool-call observation with an LLM-chosen limit."""
    return {
        "id": obs_id,
        "name": "search_child_chunks",
        "type": "SPAN",
        "input": {"query": "졸업 요건", "limit": limit},
        "output": [],
    }


def _trace(observations: list[dict]) -> dict:
    """A trace_detail dict with an inline observations list (+ noise observations)."""
    return {
        "id": "deadbeef" * 4,
        "metadata": {"trace_id": "deadbeef"},
        "observations": [
            {"id": "obs-gen", "name": "orchestrator", "type": "GENERATION", "input": {}},
            *observations,
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_search_k — the pure helper
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_k_none_trace_uses_cli_k():
    """(a) trace_detail=None → returns the cli_k unchanged."""
    k, source = _resolve_search_k(7, None)
    assert k == 7
    assert isinstance(source, str) and source


def test_resolve_k_single_trace_limit():
    """(b) one search tool-call limit=5 → returns 5."""
    trace = _trace([_search_obs("obs-1", 5)])
    k, source = _resolve_search_k(7, trace)
    assert k == 5
    assert "5" in source


def test_resolve_k_multiple_limits_uses_max_with_provenance():
    """(c) two tool-calls with limits {5,7} → returns 7 and mentions provenance."""
    trace = _trace([_search_obs("obs-1", 5), _search_obs("obs-2", 7)])
    k, source = _resolve_search_k(3, trace)
    assert k == 7
    # Provenance: both contributing observation labels should appear in the source.
    assert "obs-1" in source and "obs-2" in source
    assert "max" in source.lower()


def test_resolve_k_no_tool_call_limits_falls_back_to_cli_k():
    """(d) trace with no search tool-call limits → falls back to cli_k."""
    # observations present, but none are search_child_chunks with a limit
    trace = _trace([])
    k, source = _resolve_search_k(7, trace)
    assert k == 7
    assert "7" in source


def test_resolve_k_three_distinct_limits_picks_max():
    """Multiple differing limits {4,6,7} → max()=7 regardless of order."""
    trace = _trace(
        [_search_obs("a", 6), _search_obs("b", 4), _search_obs("c", 7)]
    )
    k, _source = _resolve_search_k(3, trace)
    assert k == 7


def test_resolve_k_ignores_non_search_observations():
    """A non-search observation carrying a `limit` must not be treated as retrieval k."""
    trace = {
        "observations": [
            {"name": "orchestrator", "type": "GENERATION", "input": {"limit": 99}},
            _search_obs("real", 5),
        ]
    }
    k, _source = _resolve_search_k(7, trace)
    assert k == 5


def test_resolve_k_accepts_k_alias_in_input():
    """A tool-call input using `k` (not `limit`) is still recognised."""
    trace = {"observations": [{"name": "search_child_chunks", "input": {"k": 6}}]}
    k, _source = _resolve_search_k(7, trace)
    assert k == 6


# ─────────────────────────────────────────────────────────────────────────────
# argparse builder — the `search` subparser --k flag (BUG #2)
# ─────────────────────────────────────────────────────────────────────────────

def test_search_parser_k_default_is_7(parser):
    """`search` sub-command defaults --k to 7."""
    args = parser.parse_args(["search", "졸업 요건"])
    assert args.k == 7
    assert args.from_trace is None


def test_search_parser_honors_k_flag(parser):
    """--k overrides the default."""
    args = parser.parse_args(["search", "졸업 요건", "--k", "5"])
    assert args.k == 5


def test_search_parser_accepts_from_trace(parser):
    """--from-trace is parsed into args.from_trace."""
    args = parser.parse_args(["search", "q", "--from-trace", "deadbeef"])
    assert args.from_trace == "deadbeef"


# ─────────────────────────────────────────────────────────────────────────────
# Import purity — the Langfuse query layer must be a lazy in-function import
# ─────────────────────────────────────────────────────────────────────────────

def test_query_import_is_lazy_inside_cmd_search():
    """get_trace_detail is imported INSIDE cmd_search, never at module top level.

    Guards the import-purity contract (tests/debug/test_import_purity.py): the
    module must import no Langfuse/qdrant code at the top level.
    """
    import debug.repro as repro

    src = inspect.getsource(repro)
    module_top = src.split("def cmd_search", 1)[0]
    # No top-level import of the Langfuse query layer or qdrant before cmd_search.
    assert not re.search(r"^from \._query import", module_top, re.MULTILINE)
    assert not re.search(r"^from qdrant_client import", module_top, re.MULTILINE)
    assert not re.search(r"^import qdrant_client", module_top, re.MULTILINE)

    # And it IS imported lazily inside cmd_search.
    cmd_src = inspect.getsource(repro.cmd_search)
    assert "from ._query import get_trace_detail" in cmd_src
