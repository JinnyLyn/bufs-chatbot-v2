"""Integration tests for rag_agent/tools.py — ToolFactory.

ToolFactory wraps a vector-store collection and a ParentStoreManager.
These tests use fake stores (no real Qdrant/embeddings) so they run
fully offline, but they exercise the complete tool-call surface including
error paths and string formatting — hence they belong in the integration
tier to distinguish them from the micro-unit tests.

All marked @pytest.mark.integration.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration


def _make_fake_collection(results=None):
    """Return a mock vector store that returns the given similarity_search results."""
    col = MagicMock()
    col.similarity_search.return_value = results or []
    return col


def _seed_parent_store(tmp_path: Path, parent_id: str, content: str, source: str = "doc.pdf"):
    """Write a JSON file so ParentStoreManager.load_content() can find it."""
    doc = {"page_content": content, "metadata": {"source": source, "parent_id": parent_id}}
    (tmp_path / f"{parent_id}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


def _make_tool_factory(tmp_path: Path, collection):
    import sys, importlib
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    from rag_agent.tools import ToolFactory
    # Patch the ParentStoreManager to use tmp_path
    factory = ToolFactory(collection)
    # Override the store path so it reads from tmp_path
    from db.parent_store_manager import ParentStoreManager
    factory.parent_store_manager = ParentStoreManager(store_path=tmp_path)
    return factory


# ---------------------------------------------------------------------------
# search_child_chunks tool
# ---------------------------------------------------------------------------

class TestSearchChildChunksTool:
    def _make_doc(self, content, parent_id="doc_parent_0", source="doc.pdf"):
        doc = MagicMock()
        doc.page_content = content
        doc.metadata = {"parent_id": parent_id, "source": source}
        return doc

    def test_returns_no_relevant_chunks_when_empty(self, tmp_path):
        factory = _make_tool_factory(tmp_path, _make_fake_collection([]))
        tools = factory.create_tools()
        search = next(t for t in tools if t.name == "search_child_chunks")
        result = search.invoke({"query": "테스트 질문", "limit": 5})
        assert result == "NO_RELEVANT_CHUNKS"

    def test_formats_results_with_parent_id_and_source(self, tmp_path):
        docs = [self._make_doc("졸업학점은 130학점입니다.", "doc_parent_0", "notice.pdf")]
        factory = _make_tool_factory(tmp_path, _make_fake_collection(docs))
        tools = factory.create_tools()
        search = next(t for t in tools if t.name == "search_child_chunks")
        result = search.invoke({"query": "졸업학점", "limit": 5})
        assert "doc_parent_0" in result
        assert "notice.pdf" in result
        assert "졸업학점" in result

    def test_returns_retrieval_error_on_exception(self, tmp_path):
        col = MagicMock()
        col.similarity_search.side_effect = RuntimeError("Qdrant down")
        factory = _make_tool_factory(tmp_path, col)
        tools = factory.create_tools()
        search = next(t for t in tools if t.name == "search_child_chunks")
        result = search.invoke({"query": "q", "limit": 3})
        assert "RETRIEVAL_ERROR" in result


# ---------------------------------------------------------------------------
# retrieve_parent_chunks tool
# ---------------------------------------------------------------------------

class TestRetrieveParentChunksTool:
    def test_returns_parent_content(self, tmp_path):
        _seed_parent_store(tmp_path, "doc_parent_0", "전체 부모 내용입니다.")
        col = _make_fake_collection()
        factory = _make_tool_factory(tmp_path, col)
        tools = factory.create_tools()
        retrieve = next(t for t in tools if t.name == "retrieve_parent_chunks")
        result = retrieve.invoke({"parent_id": "doc_parent_0"})
        assert "전체 부모 내용입니다." in result
        assert "doc_parent_0" in result

    def test_returns_error_for_missing_parent_id(self, tmp_path):
        col = _make_fake_collection()
        factory = _make_tool_factory(tmp_path, col)
        tools = factory.create_tools()
        retrieve = next(t for t in tools if t.name == "retrieve_parent_chunks")
        result = retrieve.invoke({"parent_id": "nonexistent_parent"})
        assert "PARENT_RETRIEVAL_ERROR" in result

    def test_tool_list_contains_both_tools(self, tmp_path):
        factory = _make_tool_factory(tmp_path, _make_fake_collection())
        tools = factory.create_tools()
        names = {t.name for t in tools}
        assert "search_child_chunks" in names
        assert "retrieve_parent_chunks" in names


# ---------------------------------------------------------------------------
# rag_agent/graph.py — compile-only smoke test (offline)
# ---------------------------------------------------------------------------

class TestGraphCompile:
    def test_graph_can_be_imported_without_error(self):
        """Importing rag_agent.graph should not raise (compile-time check)."""
        try:
            import rag_agent.graph  # noqa: F401
        except ImportError as e:
            pytest.skip(f"Heavy dep missing: {e}")
        except Exception as e:
            pytest.fail(f"Graph import raised unexpected error: {e}")
