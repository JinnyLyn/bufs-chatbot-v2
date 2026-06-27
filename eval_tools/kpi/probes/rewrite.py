"""``rewrite_query`` term-drift probe — retrieval recall with rewrite ON vs OFF.

``config.py:101-109`` documents the risk: LLM query rewriting can *hurt* Korean
academic retrieval (term drift / morphology / BM25 surface-form mismatch). On
combined88 rewrite OFF beat ON on every axis (contains 81.5%->85.2%), which is
why ``REWRITE_ENABLED`` defaults OFF. This probe quantifies that drift on the
*messy* (perturbed / real-usage) set, where rewrite's term-drift can be larger
or — its design value — actually help disambiguate.

Term-drift magnitude = recall(OFF) − recall(ON), in percentage points. Positive
means rewriting *lost* retrieval (drift); negative means rewriting *helped*. The
retriever and the rewrite function are injected, so the arithmetic is pure and
unit-testable; the live LLM/index dependency stays out of the offline lane.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .tokenizer import recall_at_k


def term_drift_pp(recall_off: float, recall_on: float) -> float:
    """Recall lost to rewriting, in percentage points (positive == rewrite hurts)."""
    return (recall_off - recall_on) * 100.0


@dataclass(frozen=True)
class RewriteDrift:
    """Recall@k with rewrite OFF vs ON + the drift between them."""

    k: int
    recall_off: float
    recall_on: float
    drift_pp: float
    n_queries: int

    def as_dict(self) -> dict:
        return {
            "k": self.k,
            "recall_off": self.recall_off,
            "recall_on": self.recall_on,
            "term_drift_pp": self.drift_pp,
            "n_queries": self.n_queries,
        }


def term_drift(queries: Iterable[dict],
               retrieve_fn: Callable[[str], list[object]],
               gold_for: Callable[[object], Iterable[object]],
               rewrite_fn: Callable[[str], str],
               k: int = 10) -> RewriteDrift:
    """Mean recall@k drift (OFF − ON) over a query set.

    ``queries`` items: ``{"question": str, "parent_id": object}``.
    ``retrieve_fn`` maps a query string -> ranked doc ids (live Qdrant / stub).
    ``rewrite_fn`` maps a raw question -> its rewritten form (live LLM / stub).
    ``gold_for`` maps ``parent_id`` -> gold doc ids. Recall ON retrieves on the
    rewritten query; recall OFF on the raw question.
    """
    queries = list(queries)
    off_hits = on_hits = 0.0
    for row in queries:
        gold = gold_for(row["parent_id"])
        raw = row["question"]
        off_hits += recall_at_k(retrieve_fn(raw), gold, k)
        on_hits += recall_at_k(retrieve_fn(rewrite_fn(raw)), gold, k)
    n = len(queries)
    recall_off = off_hits / n if n else 0.0
    recall_on = on_hits / n if n else 0.0
    return RewriteDrift(
        k=k,
        recall_off=recall_off,
        recall_on=recall_on,
        drift_pp=term_drift_pp(recall_off, recall_on),
        n_queries=n,
    )
