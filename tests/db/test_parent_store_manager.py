"""Integration tests for db/parent_store_manager.py.

ParentStoreManager uses only the filesystem (JSON files) so these tests are
functionally offline — no Ollama/Qdrant/Langfuse/network required.  They are
still marked @pytest.mark.integration because they exercise real I/O and the
complete persistence round-trip (write → read → load), which sits above the
pure unit boundary.

Run with:  pytest -m integration
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.integration


def _make_manager(tmp_path: Path):
    from db.parent_store_manager import ParentStoreManager
    return ParentStoreManager(store_path=tmp_path)


def _make_doc(content: str = "본문 내용", metadata: dict | None = None):
    """Minimal stand-in for a langchain Document."""
    doc = MagicMock()
    doc.page_content = content
    doc.metadata = metadata or {"source": "test.pdf", "parent_id": "test_parent_0"}
    return doc


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

class TestParentStoreRoundTrip:
    def test_save_creates_json_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_0", "내용", {"source": "doc.pdf"})
        assert (tmp_path / "doc_parent_0.json").exists()

    def test_load_returns_saved_content(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_1", "졸업학점은 130학점입니다.", {"source": "doc.pdf"})
        data = mgr.load("doc_parent_1")
        assert data["page_content"] == "졸업학점은 130학점입니다."

    def test_load_with_json_extension_suffix(self, tmp_path):
        """load() accepts 'id.json' as well as bare 'id'."""
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_2", "내용", {})
        data = mgr.load("doc_parent_2.json")
        assert data["page_content"] == "내용"

    def test_load_content_returns_expected_keys(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_3", "내용", {"source": "doc.pdf"})
        result = mgr.load_content("doc_parent_3")
        assert "content" in result
        assert "parent_id" in result
        assert "metadata" in result
        assert result["parent_id"] == "doc_parent_3"

    def test_load_content_preserves_metadata(self, tmp_path):
        mgr = _make_manager(tmp_path)
        meta = {"source": "notice.pdf", "H1": "공지"}
        mgr.save("doc_parent_4", "공지 내용", meta)
        result = mgr.load_content("doc_parent_4")
        assert result["metadata"]["source"] == "notice.pdf"


# ---------------------------------------------------------------------------
# save_many / load_content_many
# ---------------------------------------------------------------------------

class TestBatchOperations:
    def test_save_many_writes_all_files(self, tmp_path):
        mgr = _make_manager(tmp_path)
        docs = [
            ("doc_parent_0", _make_doc("내용0")),
            ("doc_parent_1", _make_doc("내용1")),
        ]
        mgr.save_many(docs)
        assert (tmp_path / "doc_parent_0.json").exists()
        assert (tmp_path / "doc_parent_1.json").exists()

    def test_load_content_many_returns_all_items(self, tmp_path):
        mgr = _make_manager(tmp_path)
        for i in range(3):
            mgr.save(f"doc_parent_{i}", f"내용{i}", {"source": "doc.pdf"})
        results = mgr.load_content_many(["doc_parent_0", "doc_parent_1", "doc_parent_2"])
        assert len(results) == 3

    def test_load_content_many_deduplicates_ids(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_0", "내용", {})
        results = mgr.load_content_many(["doc_parent_0", "doc_parent_0"])
        assert len(results) == 1

    def test_load_content_many_sorted_by_index(self, tmp_path):
        mgr = _make_manager(tmp_path)
        for i in range(5):
            mgr.save(f"doc_parent_{i}", f"내용{i}", {})
        results = mgr.load_content_many([f"doc_parent_{i}" for i in [4, 2, 0]])
        indices = [r["parent_id"].split("_")[-1] for r in results]
        assert indices == sorted(indices, key=int)


# ---------------------------------------------------------------------------
# clear_store
# ---------------------------------------------------------------------------

class TestClearStore:
    def test_clear_store_removes_all_json_files(self, tmp_path):
        mgr = _make_manager(tmp_path)
        for i in range(3):
            mgr.save(f"doc_parent_{i}", f"내용{i}", {})
        mgr.clear_store()
        assert list(tmp_path.glob("*.json")) == []

    def test_clear_store_preserves_directory(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.save("doc_parent_0", "내용", {})
        mgr.clear_store()
        assert tmp_path.is_dir()
