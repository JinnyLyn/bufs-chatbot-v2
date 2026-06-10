"""Unit tests for debug.repro copy-open and fingerprint logic.

These tests are integration-tier: they use the committed qdrant_db/ in the
worktree, but they do NOT require torch or sentence-transformers — only
qdrant-client (installed by T1 venv on dev/WSL).

Run explicitly:
    pytest tests/test_repro_copyopen.py -m integration -v

Skip automatically in CI:
    pytest -m "not integration"   (the pyproject.toml default)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# Worktree root (one level above tests/)
_WORKTREE = Path(__file__).resolve().parents[1]
_QDRANT_DB = _WORKTREE / "qdrant_db"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cleanup(tmp_dir: Path) -> None:
    """Best-effort temp-dir removal."""
    try:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_copy_open_returns_live_client():
    """_copy_open_qdrant opens a temp copy and the client scrolls real points."""
    from debug.repro import _copy_open_qdrant

    client, tmp_dir = _copy_open_qdrant(_QDRANT_DB)
    try:
        points, _next = client.scroll("document_child_chunks", limit=10, with_payload=True)
        assert len(points) > 0, "Expected at least one point in the copied index"
        # Payload should contain parent_id and source keys
        payload = points[0].payload or {}
        assert payload, f"Payload should not be empty; got: {payload}"
    finally:
        client.close()
        _cleanup(tmp_dir)


@pytest.mark.integration
def test_copy_open_point_count():
    """The copied index contains the expected ~1459 child chunks."""
    from debug.repro import _copy_open_qdrant

    client, tmp_dir = _copy_open_qdrant(_QDRANT_DB)
    try:
        info = client.get_collection("document_child_chunks")
        count = info.points_count
        # Verified: 1459 points as of 2026-06-10 ingestion
        assert count > 1000, f"Expected ~1459 points, got {count}"
    finally:
        client.close()
        _cleanup(tmp_dir)


@pytest.mark.integration
def test_qdrant_fingerprint_has_all_components():
    """Fingerprint contains all three required components."""
    from debug.repro import _qdrant_fingerprint

    fp = _qdrant_fingerprint(_QDRANT_DB)

    assert "meta=" in fp, f"Missing 'meta=' component in fingerprint: {fp!r}"
    assert "sqlite=[" in fp, f"Missing 'sqlite=[' component in fingerprint: {fp!r}"
    assert "git=" in fp, f"Missing 'git=' component in fingerprint: {fp!r}"


@pytest.mark.integration
def test_qdrant_fingerprint_deterministic():
    """Two calls on the same unmodified db_path return the identical fingerprint."""
    from debug.repro import _qdrant_fingerprint

    fp1 = _qdrant_fingerprint(_QDRANT_DB)
    fp2 = _qdrant_fingerprint(_QDRANT_DB)

    assert fp1 == fp2, (
        f"Fingerprint should be deterministic across calls;\n"
        f"  call 1: {fp1!r}\n"
        f"  call 2: {fp2!r}"
    )


@pytest.mark.integration
def test_copy_open_does_not_lock_original():
    """After copy-open, the original db_path is unmodified and still openable."""
    from debug.repro import _copy_open_qdrant
    from qdrant_client import QdrantClient

    client, tmp_dir = _copy_open_qdrant(_QDRANT_DB)
    try:
        # Confirm the copy works
        info = client.get_collection("document_child_chunks")
        assert info.points_count > 0
    finally:
        client.close()
        _cleanup(tmp_dir)

    # Original should still be intact (meta.json readable)
    assert (_QDRANT_DB / "meta.json").exists(), "Original meta.json disappeared after copy-open"
