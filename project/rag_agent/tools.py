import logging
import time
from typing import Annotated, List
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from qdrant_client.http import models as qmodels
import config
from db.parent_store_manager import ParentStoreManager

logger = logging.getLogger(__name__)


def _split_hybrid_search_raw(vs, dense_query: str, sparse_query: str, k: int, score_threshold: float):
    """Split-path hybrid core: returns raw Qdrant points (with .score) for blending support.

    Mirrors langchain_qdrant's RetrievalMode.HYBRID query exactly — the only difference is
    both legs are embedded from DIFFERENT texts. Returns (points, docs) so callers that need
    RRF scores for CE+RRF blending can use points[i].score alongside docs[i].
    """
    if getattr(vs, "embeddings", None) is None or getattr(vs, "sparse_embeddings", None) is None:
        raise ValueError(
            "SPLIT_PATH_ENABLED requires a HYBRID collection with both a dense and a sparse "
            "leg, but the vector store is missing one of them (embeddings / sparse_embeddings "
            "is None). Disable SPLIT_PATH_ENABLED or rebuild the collection in RetrievalMode.HYBRID."
        )
    dense_vec = vs.embeddings.embed_query(dense_query)
    sparse_vec = vs.sparse_embeddings.embed_query(sparse_query)
    points = vs.client.query_points(
        collection_name=vs.collection_name,
        prefetch=[
            qmodels.Prefetch(using=vs.vector_name, query=dense_vec, limit=k),
            qmodels.Prefetch(
                using=vs.sparse_vector_name,
                query=qmodels.SparseVector(indices=sparse_vec.indices, values=sparse_vec.values),
                limit=k,
            ),
        ],
        query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
        limit=k,
        score_threshold=score_threshold,
        with_payload=True,
    ).points
    docs = [
        vs._document_from_point(p, vs.collection_name, vs.content_payload_key, vs.metadata_payload_key)
        for p in points
    ]
    return points, docs


def _split_hybrid_search(vs, dense_query: str, sparse_query: str, k: int, score_threshold: float):
    """Split-path hybrid (issue #66): dense leg ← dense_query, sparse leg ← sparse_query.

    Thin wrapper over _split_hybrid_search_raw for callers that don't need RRF scores.
    When dense_query == sparse_query the result is identical to vs.similarity_search().
    """
    _, docs = _split_hybrid_search_raw(vs, dense_query, sparse_query, k, score_threshold)
    return docs


def _apply_semester_scope(docs: list, question: str, limit: int) -> list:
    """Demote wrong-semester chunks, then cut to `limit`. Never raises into the search path.

    The question used for scoping is the ORIGINAL user text, not the agent's paraphrase —
    the agent routinely drops the "2학기" qualifier when it rewrites a query (24 such cases
    in the #80 forensics), which is precisely the signal being read here.
    """
    import datetime as _dt

    from rag_agent import semester as _sem

    try:
        today = _dt.date.fromisoformat(config.SEMESTER_TODAY) if config.SEMESTER_TODAY else None
        target = _sem.target_semester(question, today)
        ordered = _sem.demote_other_semesters(docs, target)
        logger.debug("semester scope: target=%d pool=%d -> limit=%d", target, len(docs), limit)
        return ordered[:limit]
    except Exception:
        # A scoping bug must never turn into a retrieval outage — fall back to raw ranking.
        logger.exception("semester scoping failed; falling back to unscoped top-%d", limit)
        return docs[:limit]


def _budget_exceeded(state: dict) -> bool:
    """#89 elapsed-budget check shared by both retrieval tools. Never raises.

    loop_started_at is a time.monotonic() reference armed by the orchestrator's first
    turn (in-process InMemorySaver — monotonic is valid across the fan-out threads).
    A negative elapsed means the reference came from another boot/process (a durable-
    checkpointer future); fail OPEN with a warning rather than refusing every search.
    """
    if config.TOOL_CALL_SOFT_TIMEOUT_S <= 0:
        return False
    started = (state or {}).get("loop_started_at") or 0.0
    if not started:
        return False
    elapsed = time.monotonic() - started
    if elapsed < 0:
        logger.warning("loop_started_at is from another clock domain (elapsed=%.1fs) — "
                       "budget check disabled for this call", elapsed)
        return False
    if elapsed > config.TOOL_CALL_SOFT_TIMEOUT_S:
        logger.info("search budget exceeded (%.1fs > %.0fs) — refusing retrieval",
                    elapsed, config.TOOL_CALL_SOFT_TIMEOUT_S)
        return True
    return False


class ToolFactory:

    def __init__(self, collection):
        self.collection = collection
        self.parent_store_manager = ParentStoreManager()
    
    def _search_child_chunks(self, query: str, limit: int,
                             state: Annotated[dict, InjectedState] = None) -> str:
        """Search for the top K most relevant child chunks.

        Args:
            query: Search query string
            limit: Maximum number of results to return
        """
        try:
            # Latency guardrail (#89): past the elapsed budget, refuse further searches —
            # the marker tells the orchestrator to answer from what it already collected.
            # Defense-in-depth behind the edges-level cut (route_after_orchestrator_call
            # forces fallback_response on the next tool request): this fires when the
            # budget crosses mid-ToolNode batch. The marker is never evidence
            # (edges._has_tool_evidence), never a source (api/sources), and never enters
            # the synthesis/compression prompts (nodes.py filters).
            if _budget_exceeded(state):
                return ("SEARCH_BUDGET_EXCEEDED: 검색 시간 예산을 초과했습니다. "
                        "추가 검색 없이 현재까지 수집된 컨텍스트로 답하세요.")

            # Split-path retrieval (issue #66): route the user's ORIGINAL question to the
            # surface-sensitive sparse leg and the agent's query to the dense leg. The agent
            # subgraph's AgentState carries the original (pre-agent-paraphrase) question as
            # `question` (= rewrittenQuestions[idx]; with rewrite off this is the user's
            # message). `state` is injected via InjectedState and is hidden from the LLM schema.
            original = (state or {}).get("question", "") or query
            # Semester scoping (학기 교차 오염): fetch a deeper pool so there is something to
            # promote in place of the demoted wrong-semester chunks, then cut back to `limit`
            # AFTER demotion. Scoping off ⇒ fetch_k == limit and the path is byte-identical.
            fetch_k = limit * config.SEMESTER_POOL_FACTOR if config.SEMESTER_FILTER_ENABLED else limit
            if config.RERANK_ENABLED:
                # Rerank path (#104): fetch a DEEPER pool at threshold 0 (so rank_cut chunks
                # buried below the 0.3 RRF cutoff survive to the reranker), then let the
                # cross-encoder re-score against the ORIGINAL question pick the top `limit`.
                # When RERANK_BLEND_ALPHA is set, blends CE score with the RRF score to
                # prevent full override of literal-match (BM25-driven) chunks.
                from db import reranker
                pool_k = max(fetch_k, config.RERANK_PREFETCH_K)
                if config.SPLIT_PATH_ENABLED:
                    if config.RERANK_BLEND_ALPHA is not None:
                        pts, pool = _split_hybrid_search_raw(
                            self.collection, dense_query=query, sparse_query=original,
                            k=pool_k, score_threshold=0.0)
                        rrf_scores = [p.score for p in pts]
                    else:
                        pool = _split_hybrid_search(
                            self.collection, dense_query=query, sparse_query=original,
                            k=pool_k, score_threshold=0.0)
                        rrf_scores = None
                else:
                    pool = self.collection.similarity_search(query, k=pool_k, score_threshold=0.0)
                    rrf_scores = None
                if rrf_scores is None and config.RERANK_BLEND_ALPHA is not None:
                    logger.warning(
                        "RERANK_BLEND_ALPHA=%.2f has no effect without SPLIT_PATH_ENABLED "
                        "(no RRF scores available); falling back to pure CE rerank.",
                        config.RERANK_BLEND_ALPHA,
                    )
                results = reranker.rerank(original, pool, fetch_k,
                                          rrf_scores=rrf_scores, blend_alpha=config.RERANK_BLEND_ALPHA)
            elif config.SPLIT_PATH_ENABLED:
                results = _split_hybrid_search(
                    self.collection, dense_query=query, sparse_query=original,
                    k=fetch_k, score_threshold=config.SEARCH_SCORE_THRESHOLD)
            else:
                results = self.collection.similarity_search(query, k=fetch_k, score_threshold=config.SEARCH_SCORE_THRESHOLD)

            if config.SEMESTER_FILTER_ENABLED:
                results = _apply_semester_scope(results, original, limit)

            if not results:
                return "NO_RELEVANT_CHUNKS"

            return "\n\n".join([
                f"Parent ID: {doc.metadata.get('parent_id', '')}\n"
                f"File Name: {doc.metadata.get('source', '')}\n"
                f"Content: {doc.page_content.strip()}"
                for doc in results
            ])            

        except Exception:
            # Do not let the failure vanish into the returned string: a split-path API
            # drift (qdrant-client/langchain-qdrant bump) or a DENSE-only collection would
            # otherwise degrade to RETRIEVAL_ERROR with no server-side trace. The detail
            # stays in the server log only — the returned string is a stable marker so
            # internal error text never reaches the LLM/user.
            logger.exception(
                "search_child_chunks failed (split_path=%s)", config.SPLIT_PATH_ENABLED
            )
            return "RETRIEVAL_ERROR: search failed, see server log"
    
    def _retrieve_many_parent_chunks(self, parent_ids: List[str]) -> str:
        """Retrieve full parent chunks by their IDs.
    
        Args:
            parent_ids: List of parent chunk IDs to retrieve
        """
        try:
            ids = [parent_ids] if isinstance(parent_ids, str) else list(parent_ids)
            raw_parents = self.parent_store_manager.load_content_many(ids)
            if not raw_parents:
                return "NO_PARENT_DOCUMENTS"

            return "\n\n".join([
                f"Parent ID: {doc.get('parent_id', 'n/a')}\n"
                f"File Name: {doc.get('metadata', {}).get('source', 'unknown')}\n"
                f"Content: {doc.get('content', '').strip()}"
                for doc in raw_parents
            ])

        except Exception:
            logger.exception("retrieve_many_parent_chunks failed")
            return "PARENT_RETRIEVAL_ERROR: retrieval failed, see server log"

    def _retrieve_parent_chunks(self, parent_id: str,
                                state: Annotated[dict, InjectedState] = None) -> str:
        """Retrieve full parent chunks by their IDs.

        Args:
            parent_id: Parent chunk ID to retrieve
        """
        try:
            # #89: parent pulls are budget-gated too — post-budget parents inflate the
            # context and trigger the expensive compress_context node, the exact tail
            # the lever exists to cut.
            if _budget_exceeded(state):
                return ("SEARCH_BUDGET_EXCEEDED: 검색 시간 예산을 초과했습니다. "
                        "추가 검색 없이 현재까지 수집된 컨텍스트로 답하세요.")
            parent = self.parent_store_manager.load_content(parent_id)
            if not parent:
                return "NO_PARENT_DOCUMENT"

            return (
                f"Parent ID: {parent.get('parent_id', 'n/a')}\n"
                f"File Name: {parent.get('metadata', {}).get('source', 'unknown')}\n"
                f"Content: {parent.get('content', '').strip()}"
            )

        except Exception:
            logger.exception("retrieve_parent_chunks failed (parent_id=%s)", parent_id)
            return "PARENT_RETRIEVAL_ERROR: retrieval failed, see server log"

    def create_tools(self) -> List:
        """Create and return the list of tools."""
        search_tool = tool("search_child_chunks")(self._search_child_chunks)
        retrieve_tool = tool("retrieve_parent_chunks")(self._retrieve_parent_chunks)
        
        return [search_tool, retrieve_tool]