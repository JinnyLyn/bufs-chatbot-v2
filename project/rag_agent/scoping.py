"""Shared demote-never-delete selection mechanics for retrieval scoping levers.

Extracted from rag_agent.semester (#178) so multiple scoping levers (학기 스코프,
OCU 스코프) demote through ONE selection pass with a combined predicate. Sequential
per-lever passes would break the "one sub-threshold admission per demotion"
bookkeeping: the second pass has no scores left to re-apply the threshold with,
and each pass would grant its own admissions.

The contract (inherited verbatim from the semester lever, see rag_agent.semester
module docstring #1 and #178):

- **Demote, never delete.** Chunks matching ``is_demoted`` go to the back of the
  candidate list rather than being dropped, so a question whose evidence genuinely
  lives in a demoted chunk still finds it instead of hitting NO_RELEVANT_CHUNKS.
- The score threshold is enforced at selection time over a pool fetched at
  threshold 0.0 — demoted chunks must clear it too, and only ever backfill.
- A sub-threshold non-demoted chunk is admitted only to stand in for a demoted
  one — one admission per demotion. With no demotion there is no vacancy, so an
  off-topic question still returns [] and the NO_RELEVANT_CHUNKS → refusal
  routing (edges.py) keeps working exactly as with the levers OFF.
"""

from __future__ import annotations

from typing import Callable


def select_scoped(
    scored_docs: list, is_demoted: Callable, limit: int, score_threshold: float
) -> list:
    """Final selection over a deep pool fetched WITHOUT a score cutoff.

    ``scored_docs`` is a ranked list of ``(doc, score)`` pairs; ``is_demoted``
    maps a doc to True when it should rank behind everything else.
    """
    keep, demote, standby = [], [], []
    for doc, score in scored_docs:
        if is_demoted(doc):
            if score >= score_threshold:
                demote.append(doc)
        elif score >= score_threshold:
            keep.append(doc)
        else:
            standby.append(doc)
    keep += standby[: len(demote)]
    return (keep + demote)[:limit]


def demote_scoped(docs: list, is_demoted: Callable) -> list:
    """Stable reorder: non-demoted docs first, demoted last.

    Order *within* each group is preserved, so the retriever's (or reranker's)
    ranking still decides everything except the scoping split.
    """
    keep, demote = [], []
    for d in docs:
        (demote if is_demoted(d) else keep).append(d)
    return keep + demote
