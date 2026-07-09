"""Cross-encoder re-ranker (issue #104).

Re-scores retrieved child chunks against the user's ORIGINAL question and returns them
in descending relevance. Kept separate from the bi-encoder embeddings (VectorDbManager)
so the retrieval path can toggle it via config.RERANK_ENABLED without touching the vector
store. The model is loaded eagerly at server startup (server.py lifespan) so that
every request hits an already-resident model — no cold-start on the request path.

Why the ORIGINAL question is the rerank key, not the agent's tool-call query: the buried
answer chunks we target (rank_cut) and the drift-lost chunks (#87) are relevant to what the
USER asked, not to the agent's reformulation. Scoring against the original recovers both.
"""
import logging
import time
import config

logger = logging.getLogger(__name__)

_reranker = None


def get_reranker():
    """Singleton CrossEncoder (config.RERANK_MODEL on config.RERANK_DEVICE).

    Call once at server startup (server.py lifespan) to pre-load the model.
    Subsequent calls return the cached instance in O(1).
    """
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(config.RERANK_MODEL, device=config.RERANK_DEVICE)
    return _reranker


def rerank(query, docs, top_k, *, rrf_scores=None, blend_alpha=None):
    """Return the top_k `docs` re-scored against `query`, highest relevance first.

    When rrf_scores and blend_alpha are provided, final score =
    blend_alpha * CE_norm + (1-blend_alpha) * RRF_norm (min-max normalised per call).
    This prevents the cross-encoder from fully overriding BM25-matched chunks that had
    high RRF scores — the "literal-match 실종" regression class.

    When config.RERANK_SCORE_MIN is set, candidates scoring below it are dropped (so an
    all-irrelevant pool yields an empty list → NO_RELEVANT_CHUNKS upstream). With no floor
    the top_k are returned unconditionally (rerank order still applied).

    Logs pure inference latency (model.predict wall-clock, excluding model load) so
    warm-up vs inference costs can be separated in the server log.
    """
    if not docs:
        return docs
    model = get_reranker()
    t0 = time.perf_counter()
    ce_scores = list(model.predict([(query, d.page_content) for d in docs]))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("rerank: %d pairs → %.0fms", len(docs), elapsed_ms)

    if blend_alpha is not None and rrf_scores is not None and len(rrf_scores) == len(ce_scores):
        def _minmax(xs):
            mn, mx = min(xs), max(xs)
            return [0.5] * len(xs) if mx == mn else [(x - mn) / (mx - mn) for x in xs]
        ce_norm = _minmax(ce_scores)
        rrf_norm = _minmax(rrf_scores)
        final_scores = [blend_alpha * ce + (1 - blend_alpha) * rrf
                        for ce, rrf in zip(ce_norm, rrf_norm)]
        logger.info("rerank blend: alpha=%.2f", blend_alpha)
    else:
        final_scores = ce_scores

    ranked = sorted(zip(docs, final_scores, ce_scores), key=lambda t: -t[1])
    if config.RERANK_SCORE_MIN is not None:
        ranked = [t for t in ranked if t[2] >= config.RERANK_SCORE_MIN]
    return [doc for doc, _, _ in ranked[:top_k]]
