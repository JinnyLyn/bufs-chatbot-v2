import logging
from typing import Annotated, List
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from qdrant_client.http import models as qmodels
import config
from db.parent_store_manager import ParentStoreManager

logger = logging.getLogger(__name__)


def _split_hybrid_search(vs, dense_query: str, sparse_query: str, k: int, score_threshold: float):
    """Split-path hybrid (issue #66): dense leg ← dense_query, sparse leg ← sparse_query.

    Mirrors langchain_qdrant's RetrievalMode.HYBRID query exactly (same prefetch limit=k,
    same RRF fusion, same score_threshold, same Document reconstruction) — the only
    difference is the two legs are embedded from DIFFERENT texts. When dense_query ==
    sparse_query the result is identical to vs.similarity_search(query, k, score_threshold).
    """
    # Split-path drives both legs, so it needs a HYBRID store: dense embeddings AND
    # sparse_embeddings. On a DENSE-only (or sparse-only) collection one of these is None,
    # which would blow up with a cryptic AttributeError on .embed_query below.
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
    return [
        vs._document_from_point(p, vs.collection_name, vs.content_payload_key, vs.metadata_payload_key)
        for p in points
    ]


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
            # Split-path retrieval (issue #66): route the user's ORIGINAL question to the
            # surface-sensitive sparse leg and the agent's query to the dense leg. The agent
            # subgraph's AgentState carries the original (pre-agent-paraphrase) question as
            # `question` (= rewrittenQuestions[idx]; with rewrite off this is the user's
            # message). `state` is injected via InjectedState and is hidden from the LLM schema.
            original = (state or {}).get("question", "") or query
            if config.SPLIT_PATH_ENABLED:
                results = _split_hybrid_search(
                    self.collection, dense_query=query, sparse_query=original,
                    k=limit, score_threshold=config.SEARCH_SCORE_THRESHOLD)
            else:
                results = self.collection.similarity_search(query, k=limit, score_threshold=config.SEARCH_SCORE_THRESHOLD)
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

    def _retrieve_parent_chunks(self, parent_id: str) -> str:
        """Retrieve full parent chunks by their IDs.
    
        Args:
            parent_id: Parent chunk ID to retrieve
        """
        try:
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