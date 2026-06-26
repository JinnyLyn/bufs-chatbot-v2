"""Retrieval-depth KPI runner — recall@k / MRR.

LIVE-INDEX ONLY
---------------
This runner requires a live ``QdrantClient`` + dense embeddings.  It MUST be
skipped on the ``--from-predictions`` path because ``results[]`` in a
prediction dump is the *post-agent rendered citation panel*, not the
retriever's raw top-k:

  * ``project/api/sources.py:_new_result`` (`:26-41`) hardcodes
    ``score: 0.0`` (``:29``) and ``text[:600]`` (``:28``).
  * ``parse_tool_results`` (`:44-75`) caps at ``max_items=10``, dedupes on
    ``(source, text[:80])``, and assembles already-surfaced post-compression
    tool output.

Real recall@k / mrr requires ``similarity_search_with_score`` on the live
index.  The runner does NOT need a backend or LLM — only Qdrant + a dense
embedding model.

Usage
-----
On the ``--from-predictions`` path the caller MUST pass
``from_predictions=True``. The runner raises :class:`RetrievalSkipError`
immediately, before importing any live deps.  The gate handles this as
``SKIPPED (requires live index)`` — NOT a NO-GO.

With ``--require-retrieval`` and ``from_predictions=True`` the gate should
convert the skip to an ERROR (exit 2); that policy lives in the gate, not here.

Ported from ``eval_tools/_retrieval_recall.py``, de-Windows-hardcoded and
adapted to the canonical scorer's corrected fact-extraction logic.

Integration
-----------
The live path (``from_predictions=False``) imports ``langchain_huggingface``,
``langchain_qdrant``, and ``qdrant_client`` lazily — only when actually called.
Tests that exercise the live path must be marked ``@pytest.mark.integration``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Gold-fact extraction / matching is the SAME corrected lineage as the scorer.
# Import it (scorer is pure / dependency-free) rather than keep a
# divergence-prone copy (I2). Aliased to the private names callers already use.
from eval_tools.kpi.scorer import extract_facts as _extract_facts, matched as _matched


# ---------------------------------------------------------------------------
# Public exception — callers catch this to handle SKIP
# ---------------------------------------------------------------------------

class RetrievalSkipError(RuntimeError):
    """Raised when retrieval-depth is requested on the ``--from-predictions`` path.

    The gate treats this as ``SKIPPED (requires live index)``, never as a
    NO-GO.  Raise also when Qdrant is unreachable on a live run (the gate
    converts that to SKIPPED unless ``--require-retrieval`` is set).
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievalResult:
    """Recall / MRR metrics from a live retrieval evaluation.

    All float fields are rounded to 4 decimal places.
    ``n_questions`` is the count of answerable questions whose
    ``ground_truth`` yielded at least one extractable fact (those without
    extractable facts are excluded from recall/mrr — no gold signal).
    """

    k: int
    recall: float    # recall@k (strict: ALL facts in the top-k chunk)
    mrr: float       # mean reciprocal rank of first all-facts chunk
    coverage: float  # mean best single-chunk fact coverage
    n_questions: int


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    records: Iterable[dict],
    *,
    qdrant_url: str,
    collection: str,
    dense_model: str,
    k: int = 10,
    from_predictions: bool = False,
) -> RetrievalResult:
    """Compute recall@k / MRR against a live Qdrant index.

    Parameters
    ----------
    records:
        Prediction-dump records (need ``question``, ``ground_truth``,
        ``answerable``).  Only answerable records with extractable facts
        are scored.
    qdrant_url:
        URL to a live Qdrant instance, e.g. ``http://localhost:6333``.
        Never hardcoded — passed by the caller from the active profile.
    collection:
        Qdrant collection name.
    dense_model:
        HuggingFace model name for dense embeddings, e.g.
        ``"BAAI/bge-m3"``.  No hardcoded default — provided by profile.
    k:
        Top-k cutoff for recall / coverage (default 10).
    from_predictions:
        When ``True``, raises :class:`RetrievalSkipError` immediately and
        never touches live deps.  Callers on the ``--from-predictions``
        path MUST pass this flag.

    Returns
    -------
    RetrievalResult

    Raises
    ------
    RetrievalSkipError
        When ``from_predictions=True``.
    RuntimeError
        When required live dependencies are not installed.
    """
    if from_predictions:
        raise RetrievalSkipError(
            "retrieval-depth requires a live Qdrant index and cannot be "
            "derived from a prediction dump (results[] is the post-agent "
            "rendered citation panel: scores == 0.0, text truncated to 600 "
            "chars, capped/deduped). SKIPPED on --from-predictions path."
        )

    # Lazy import — only executed when a live run is requested.
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        from langchain_qdrant import QdrantVectorStore, RetrievalMode
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise RuntimeError(
            "retrieval runner requires langchain-huggingface, langchain-qdrant,"
            f" and qdrant-client to be installed: {exc}"
        ) from exc

    # Build scored items: answerable questions with extractable gold facts.
    items: list[tuple[str, set[str]]] = []
    for r in records:
        if not r.get("answerable"):
            continue
        facts = _extract_facts(r.get("ground_truth", ""))
        if facts:
            items.append((str(r.get("question", "")), facts))

    if not items:
        return RetrievalResult(k=k, recall=0.0, mrr=0.0, coverage=0.0, n_questions=0)

    client = QdrantClient(url=qdrant_url)
    dense = HuggingFaceEmbeddings(
        model_name=dense_model,
        model_kwargs={"device": "cpu"},
    )
    store = QdrantVectorStore(
        client=client,
        collection_name=collection,
        embedding=dense,
        retrieval_mode=RetrievalMode.DENSE,
    )

    recall_hits = 0
    rr_sum = 0.0
    cov_sum = 0.0

    for question, facts in items:
        docs = [d for d, _ in store.similarity_search_with_score(question, k=k)]
        best_cov = 0.0
        first_all_rank: int | None = None

        for rank, doc in enumerate(docs, 1):
            hit = sum(1 for f in facts if _matched(f, doc.page_content))
            cov = hit / len(facts)
            best_cov = max(best_cov, cov)
            if hit == len(facts) and first_all_rank is None:
                first_all_rank = rank

        cov_sum += best_cov
        if first_all_rank is not None:
            recall_hits += 1
            rr_sum += 1.0 / first_all_rank

    client.close()

    n = len(items)
    return RetrievalResult(
        k=k,
        recall=round(recall_hits / n, 4),
        mrr=round(rr_sum / n, 4),
        coverage=round(cov_sum / n, 4),
        n_questions=n,
    )
