"""Security regression guard for db/parent_store_manager.py (issue #17).

These are HERMETIC unit tests — they touch only the filesystem (tmp_path) and
require no Ollama/Qdrant/Langfuse/network.  They are deliberately kept OUT of
the @pytest.mark.integration sibling module (test_parent_store_manager.py) so
that the required per-PR unit gate (`pytest -m "not integration"`) actually
runs them: path-traversal containment must be enforced on every PR, not only
on the manual self-hosted runner.

parent_id originates from an LLM tool argument, so load/save must reject any id
that escapes the store directory.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _make_manager(tmp_path: Path):
    from db.parent_store_manager import ParentStoreManager
    return ParentStoreManager(store_path=tmp_path)


class TestPathTraversalContainment:
    def test_load_rejects_relative_traversal(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.load("../../../etc/passwd")

    def test_load_rejects_absolute_path(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.load("/etc/passwd")

    def test_save_rejects_relative_traversal(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.save("../escape", "내용", {})

    def test_valid_id_round_trip_still_works(self, tmp_path):
        mgr = _make_manager(tmp_path)
        saved = {"page_content": "졸업학점은 130학점입니다.", "metadata": {"source": "doc.pdf"}}
        mgr.save("doc_parent_1", saved["page_content"], saved["metadata"])
        assert mgr.load("doc_parent_1") == saved

    def test_valid_id_with_explicit_json_suffix_loads(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_1", "내용", {"source": "doc.pdf"})
        assert mgr.load("doc_parent_1.json")["page_content"] == "내용"

    @pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
    def test_load_rejects_symlink_escape(self, tmp_path):
        # Pins the invariant that containment depends on resolve() following
        # symlinks: a symlink inside the store pointing outside it must NOT be a
        # usable escape hatch. A future swap to os.path.normpath (which does NOT
        # follow symlinks) would silently reopen the hole and fail this test.
        mgr = _make_manager(tmp_path)
        outside = tmp_path.parent / "outside_target"
        outside.mkdir()
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        # "link/secret" -> resolves through the symlink to outside/secret.json,
        # which is outside the store root -> rejected before any read (the
        # external file need not exist; resolve(strict=False) still follows the
        # existing symlink dir).
        with pytest.raises(ValueError):
            mgr.load("link/secret")

    def test_save_rejects_absolute_path(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.save("/etc/passwd", "x", {})
