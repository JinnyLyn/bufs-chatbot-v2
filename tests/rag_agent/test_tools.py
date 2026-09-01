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


# ---------------------------------------------------------------------------
# #89 — TOOL_CALL_SOFT_TIMEOUT_S elapsed soft cap
# ---------------------------------------------------------------------------

class TestSearchBudgetSoftTimeout:
    def _search(self, tmp_path, col):
        factory = _make_tool_factory(tmp_path, col)
        return next(t for t in factory.create_tools() if t.name == "search_child_chunks")

    def test_exceeded_budget_refuses_search_without_touching_collection(
            self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("TOOL_CALL_SOFT_TIMEOUT_S", "90")
        col = MagicMock()
        search = self._search(tmp_path, col)
        import time as _time
        result = search.invoke({"query": "q", "limit": 5,
                                "state": {"loop_started_at": _time.monotonic() - 120}})
        assert result.startswith("SEARCH_BUDGET_EXCEEDED")
        col.similarity_search.assert_not_called()

    def test_within_budget_searches_normally(self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("TOOL_CALL_SOFT_TIMEOUT_S", "90")
        col = _make_fake_collection([])
        search = self._search(tmp_path, col)
        import time as _time
        result = search.invoke({"query": "q", "limit": 5,
                                "state": {"loop_started_at": _time.monotonic() - 1}})
        assert result == "NO_RELEVANT_CHUNKS"
        col.similarity_search.assert_called_once()

    def test_unarmed_state_never_triggers(self, tmp_path, env_isolated, monkeypatch):
        """loop_started_at=0.0(미장전)이면 예산 검사가 발화하지 않는다."""
        monkeypatch.setenv("TOOL_CALL_SOFT_TIMEOUT_S", "90")
        col = _make_fake_collection([])
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "q", "limit": 5, "state": {"loop_started_at": 0.0}})
        assert result == "NO_RELEVANT_CHUNKS"

    def test_lever_off_ignores_stale_timestamp(self, tmp_path, env_isolated):
        col = _make_fake_collection([])
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "q", "limit": 5, "state": {"loop_started_at": 1.0}})
        assert result == "NO_RELEVANT_CHUNKS"
        col.similarity_search.assert_called_once()


class TestBudgetHelperAndParentGate:
    def test_negative_elapsed_fails_open(self, tmp_path, env_isolated, monkeypatch):
        """다른 클록 도메인의 loop_started_at(음수 경과)은 검사 무력화 — 검색은 계속된다."""
        monkeypatch.setenv("TOOL_CALL_SOFT_TIMEOUT_S", "90")
        col = _make_fake_collection([])
        factory = _make_tool_factory(tmp_path, col)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        import time as _time
        result = search.invoke({"query": "q", "limit": 5,
                                "state": {"loop_started_at": _time.monotonic() + 10_000}})
        assert result == "NO_RELEVANT_CHUNKS"
        col.similarity_search.assert_called_once()

    def test_parent_retrieval_is_budget_gated(self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("TOOL_CALL_SOFT_TIMEOUT_S", "90")
        _seed_parent_store(tmp_path, "p0", "부모 내용")
        factory = _make_tool_factory(tmp_path, _make_fake_collection())
        retrieve = next(t for t in factory.create_tools() if t.name == "retrieve_parent_chunks")
        import time as _time
        result = retrieve.invoke({"parent_id": "p0",
                                  "state": {"loop_started_at": _time.monotonic() - 120}})
        assert result.startswith("SEARCH_BUDGET_EXCEEDED")
        assert retrieve.invoke({"parent_id": "p0", "state": {}}).find("부모 내용") >= 0


# ---------------------------------------------------------------------------
# semester lever wiring (#178) — rerank-OFF paths fetch deep at 0.0 with scores
# ---------------------------------------------------------------------------

class TestSemesterLeverWiring:
    def _make_doc(self, source, parent_id="p0"):
        doc = MagicMock()
        doc.page_content = "content"
        doc.metadata = {"parent_id": parent_id, "source": source}
        return doc

    def test_lever_on_plain_path_fetches_deep_at_zero_threshold(
            self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("SEMESTER_FILTER_ENABLED", "true")
        monkeypatch.setenv("SEMESTER_TODAY", "2026-08-03")  # 2학기
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc("2026학년도2학기학사안내.md", "s2a"), 0.5),
            (self._make_doc("2026학년도1학기학사안내.pdf", "s1a"), 0.45),
            (self._make_doc("2026학년도2학기학사안내.md", "s2b"), 0.25),  # threshold 미달
        ]
        factory = _make_tool_factory(tmp_path, col)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        result = search.invoke({"query": "2학기 개강일", "limit": 2})

        col.similarity_search_with_score.assert_called_once_with(
            "2학기 개강일", k=6, score_threshold=0.0)  # limit 2 × POOL_FACTOR 3
        col.similarity_search.assert_not_called()
        # 강등 1건(s1a)이 비운 슬롯을 threshold 미달 같은-학기(s2b)가 채운다
        assert "s2a" in result and "s2b" in result and "s1a" not in result

    def test_lever_on_returns_no_relevant_chunks_for_offtopic(
            self, tmp_path, env_isolated, monkeypatch):
        """강등 0건이면 승격도 0건 — 거부 라우팅용 NO_RELEVANT_CHUNKS 보존 (#178)."""
        monkeypatch.setenv("SEMESTER_FILTER_ENABLED", "true")
        monkeypatch.setenv("SEMESTER_TODAY", "2026-08-03")
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc("2026학년도2학기학사안내.md"), 0.15),
            (self._make_doc("공인결석 신청 매뉴얼.pdf"), 0.10),
        ]
        factory = _make_tool_factory(tmp_path, col)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        assert search.invoke({"query": "오늘 점심 메뉴", "limit": 5}) == "NO_RELEVANT_CHUNKS"

    def test_lever_on_split_path_uses_raw_search_with_scores(
            self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("SEMESTER_FILTER_ENABLED", "true")
        monkeypatch.setenv("SPLIT_PATH_ENABLED", "true")
        monkeypatch.setenv("SEMESTER_TODAY", "2026-08-03")
        factory = _make_tool_factory(tmp_path, MagicMock())
        import rag_agent.tools as tools_mod
        raw = MagicMock(return_value=(
            [MagicMock(score=0.5)], [self._make_doc("2026학년도2학기학사안내.md", "s2a")]))
        monkeypatch.setattr(tools_mod, "_split_hybrid_search_raw", raw)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        result = search.invoke({"query": "2학기 개강일", "limit": 5})

        assert raw.call_args.kwargs["k"] == 15  # limit 5 × POOL_FACTOR 3
        assert raw.call_args.kwargs["score_threshold"] == 0.0
        assert "s2a" in result

    def test_lever_off_plain_path_is_unchanged(self, tmp_path, env_isolated):
        docs = [self._make_doc("2026학년도1학기학사안내.pdf")]
        col = _make_fake_collection(docs)
        factory = _make_tool_factory(tmp_path, col)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        search.invoke({"query": "개강일", "limit": 5})

        col.similarity_search.assert_called_once_with("개강일", k=5, score_threshold=0.3)
        col.similarity_search_with_score.assert_not_called()

    def test_lever_off_split_path_fetches_at_limit(self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("SPLIT_PATH_ENABLED", "true")
        factory = _make_tool_factory(tmp_path, MagicMock())
        import rag_agent.tools as tools_mod
        split = MagicMock(return_value=[self._make_doc("glossary.md")])
        monkeypatch.setattr(tools_mod, "_split_hybrid_search", split)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        search.invoke({"query": "개강일", "limit": 5})

        assert split.call_args.kwargs["k"] == 5
        assert split.call_args.kwargs["score_threshold"] == 0.3

    def test_lever_on_scoping_failure_falls_back_to_thresholded_topk(
            self, tmp_path, env_isolated, monkeypatch):
        monkeypatch.setenv("SEMESTER_FILTER_ENABLED", "true")
        monkeypatch.setenv("SEMESTER_TODAY", "2026-08-03")
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc("2026학년도2학기학사안내.md", "keep"), 0.5),
            (self._make_doc("2026학년도2학기학사안내.md", "cut"), 0.1),
        ]
        factory = _make_tool_factory(tmp_path, col)
        search = next(t for t in factory.create_tools() if t.name == "search_child_chunks")
        from rag_agent import semester as sem_mod
        monkeypatch.setattr(sem_mod, "target_semester",
                            MagicMock(side_effect=RuntimeError("boom")))
        result = search.invoke({"query": "개강일", "limit": 5})
        assert "keep" in result and "cut" not in result


# ---------------------------------------------------------------------------
# OCU lever wiring — OCU-topic chunks demoted unless the question asks about OCU
# ---------------------------------------------------------------------------

class TestOcuLeverWiring:
    OCU_개강 = "- 가. OCU 개강일 : 2026.03.02.(월) 오전 10시"
    일반_개강 = "2026학년도 1학기 개강: 3월 2일(월)"

    def _make_doc(self, content, parent_id="p0", source="2026학년도1학기학사안내.pdf"):
        doc = MagicMock()
        doc.page_content = content
        doc.metadata = {"parent_id": parent_id, "source": source}
        return doc

    def _search(self, tmp_path, col):
        factory = _make_tool_factory(tmp_path, col)
        return next(t for t in factory.create_tools() if t.name == "search_child_chunks")

    def test_ocu_lever_alone_fetches_deep_and_demotes_ocu_topic_chunk(
            self, tmp_path, env_isolated, monkeypatch):
        """사용자 신고 재현: '1학기 개강일' 질문에서 OCU 개강일 청크가 limit 밖으로 강등된다."""
        monkeypatch.setenv("OCU_FILTER_ENABLED", "true")
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc(self.OCU_개강, "ocu"), 0.55),
            (self._make_doc(self.일반_개강, "gen1"), 0.50),
            (self._make_doc("수강신청 기간 안내", "gen2"), 0.45),
        ]
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "1학기 개강일 언제야?", "limit": 2})

        col.similarity_search_with_score.assert_called_once_with(
            "1학기 개강일 언제야?", k=6, score_threshold=0.0)  # limit 2 × POOL_FACTOR 3
        col.similarity_search.assert_not_called()
        assert "gen1" in result and "gen2" in result and "ocu" not in result

    def test_ocu_question_stands_the_lever_down(self, tmp_path, env_isolated, monkeypatch):
        """질문이 OCU를 명시하면 강등 없음 — thresholded top-k와 동일하게 동작한다."""
        monkeypatch.setenv("OCU_FILTER_ENABLED", "true")
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc(self.OCU_개강, "ocu"), 0.55),
            (self._make_doc(self.일반_개강, "gen1"), 0.50),
            (self._make_doc("threshold 미달 청크", "sub"), 0.1),
        ]
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "OCU 개강일은 언제인가요?", "limit": 2})
        assert "ocu" in result and "gen1" in result and "sub" not in result

    def test_both_levers_demote_through_one_selection_pass(
            self, tmp_path, env_isolated, monkeypatch):
        """학기 + OCU 강등이 결합 predicate 한 번의 선별로 함께 적용된다."""
        monkeypatch.setenv("SEMESTER_FILTER_ENABLED", "true")
        monkeypatch.setenv("OCU_FILTER_ENABLED", "true")
        monkeypatch.setenv("SEMESTER_TODAY", "2026-05-20")  # 1학기
        col = MagicMock()
        col.similarity_search_with_score.return_value = [
            (self._make_doc(self.OCU_개강, "ocu"), 0.60),                      # OCU 강등
            (self._make_doc("2학기 개강 안내", "s2",
                            source="2026학년도2학기학사안내.md"), 0.55),        # 학기 강등
            (self._make_doc(self.일반_개강, "gen1"), 0.50),
            (self._make_doc("수강신청 기간 안내", "gen2"), 0.45),
        ]
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "1학기 개강일 언제야?", "limit": 2})
        assert "gen1" in result and "gen2" in result
        assert "ocu" not in result and "s2" not in result

    def test_lever_off_never_touches_scored_search(self, tmp_path, env_isolated):
        col = _make_fake_collection([self._make_doc(self.OCU_개강, "ocu")])
        search = self._search(tmp_path, col)
        result = search.invoke({"query": "1학기 개강일 언제야?", "limit": 5})
        col.similarity_search.assert_called_once_with(
            "1학기 개강일 언제야?", k=5, score_threshold=0.3)
        col.similarity_search_with_score.assert_not_called()
        assert "ocu" in result  # 레버 OFF ⇒ 강등 없음 (기존 동작 그대로)
