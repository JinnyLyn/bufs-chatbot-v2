"""Integration tests for db/vector_db_manager.py.

These tests require:
  - qdrant-client installed (not in the minimal dev deps)
  - langchain-qdrant + langchain-huggingface installed
  - Embedding model available (downloads on first run — needs network + disk)

All tests are skipped when the heavy deps are absent.  Run only after a full
`pip install -r requirements.txt` and with sufficient disk/network access.

Skip conditions are expressed via importorskip so the test file still *collects*
cleanly, but every test is skipped in the offline CI job.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration

# Guard: skip entire module if heavy deps are absent
qdrant_client = pytest.importorskip("qdrant_client", reason="qdrant-client not installed")
langchain_qdrant = pytest.importorskip("langchain_qdrant", reason="langchain-qdrant not installed")


class TestVectorDbManagerIntegration:
    """Smoke tests that require a real local Qdrant instance (embedded, via tmp_path).

    These do NOT run against production data.  They create a temporary Qdrant
    collection on local disk, assert basic CRUD works, then clean up.
    """

    @pytest.fixture()
    def tmp_qdrant_path(self, tmp_path):
        return str(tmp_path / "qdrant_test")

    @pytest.fixture()
    def manager(self, tmp_qdrant_path, monkeypatch):
        monkeypatch.setenv("QDRANT_DB_PATH", tmp_qdrant_path)
        import importlib, sys
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])
        from db.vector_db_manager import VectorDbManager
        return VectorDbManager()

    def test_create_collection_does_not_raise(self, manager):
        """Collection creation should succeed without error."""
        manager.create_collection("test_collection")

    def test_collection_exists_after_create(self, manager):
        manager.create_collection("test_collection_2")
        assert manager._VectorDbManager__client.collection_exists("test_collection_2")

    def test_delete_collection_removes_it(self, manager):
        manager.create_collection("to_delete")
        manager.delete_collection("to_delete")
        assert not manager._VectorDbManager__client.collection_exists("to_delete")

    def test_delete_nonexistent_collection_does_not_raise(self, manager):
        manager.delete_collection("ghost_collection")
