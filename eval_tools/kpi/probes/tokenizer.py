"""Kiwi tokenizer robustness probe — token stability (offline) + recall drop (live).

The Korean sparse leg uses a Kiwi morphological tokenizer
(``project/db/korean_sparse.py``). Typo'd / mis-spaced queries can fragment
differently (``졸업요건은`` vs ``졸업 요건은`` vs a jamo typo), changing the BM25
surface forms and dropping sparse recall. Two measurements:

* **token stability (PURE, no index)** — compare the tokenizer's output on a
  clean query vs a perturbed sibling (Jaccard + retention). Quantifies how much
  a perturbation churns the token set *before* any retrieval. Runs offline; the
  tokenizer is injectable so the default offline lane needs no JVM / no kiwipiepy.
* **recall_drop_pp (LIVE, needs Qdrant)** — recall@k on perturbed vs clean
  queries against the live index. The *arithmetic* here is pure and stubbable;
  the retriever is injected, so the live dependency stays out of the unit lane.

``config``-free at import time: the live kiwi tokenizer and any index access are
imported lazily inside the live helpers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

# Offline default tokenizer: a dependency-free clone of FastEmbed's SimpleTokenizer
# (lowercase -> non-word becomes space -> split), mirroring korean_sparse's
# "whitespace" control. Good enough to *measure churn*; the live probe swaps in
# the real Kiwi tokenizer.
_WS_NONWORD = re.compile(r"[^\w]", re.UNICODE)


def default_offline_tokenizer(text: str) -> list[str]:
    """Dependency-free whitespace tokenizer (FastEmbed SimpleTokenizer clone)."""
    return _WS_NONWORD.sub(" ", text.lower()).split()


def kiwi_tokenizer() -> Callable[[str], list[str]]:
    """Return the live Kiwi content-morpheme tokenizer (lazy import; needs kiwipiepy).

    Reuses the project's ``KoreanBM25Sparse('kiwi')`` tokenizer so the probe sees
    exactly the morphemes the sparse retriever indexes. Integration-only.
    """
    from db.korean_sparse import _KiwiTokenizer  # lazy: avoids JVM/kiwipiepy in unit lane

    return _KiwiTokenizer()


@dataclass(frozen=True)
class TokenStability:
    """How much a perturbation churns a tokenizer's output.

    ``jaccard`` |clean ∩ var| / |clean ∪ var| (1.0 == identical token sets).
    ``retained`` fraction of clean tokens that survive in the variant.
    """

    clean_tokens: tuple[str, ...]
    variant_tokens: tuple[str, ...]
    shared: int
    jaccard: float
    retained: float
    dropped: tuple[str, ...]
    added: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "clean_tokens": list(self.clean_tokens),
            "variant_tokens": list(self.variant_tokens),
            "shared": self.shared,
            "jaccard": self.jaccard,
            "retained": self.retained,
            "dropped": list(self.dropped),
            "added": list(self.added),
        }


def token_stability(clean: str, variant: str,
                    tokenizer: Callable[[str], list[str]] = default_offline_tokenizer) -> TokenStability:
    """Compare ``tokenizer`` output on a clean query vs a perturbed sibling."""
    clean_toks = tokenizer(clean)
    var_toks = tokenizer(variant)
    cset, vset = set(clean_toks), set(var_toks)
    shared = cset & vset
    union = cset | vset
    jaccard = len(shared) / len(union) if union else 1.0
    retained = len(shared) / len(cset) if cset else 1.0
    return TokenStability(
        clean_tokens=tuple(clean_toks),
        variant_tokens=tuple(var_toks),
        shared=len(shared),
        jaccard=jaccard,
        retained=retained,
        dropped=tuple(sorted(cset - vset)),
        added=tuple(sorted(vset - cset)),
    )


def recall_at_k(retrieved_ids: Iterable[object], gold_ids: Iterable[object], k: int = 10) -> float:
    """Binary recall@k: 1.0 if any gold id appears in the top-``k`` retrieved, else 0.0.

    Matches the existing ``_retrieval_recall`` "did the right doc surface in
    top-k" convention (per-query hit, averaged over the set).
    """
    gold = set(gold_ids)
    if not gold:
        return 0.0
    topk = list(retrieved_ids)[:k]
    return 1.0 if any(rid in gold for rid in topk) else 0.0


def recall_drop_pp(clean_recall: float, perturbed_recall: float) -> float:
    """Recall lost to perturbation, in percentage points (positive == worse)."""
    return (clean_recall - perturbed_recall) * 100.0


@dataclass(frozen=True)
class RecallDrop:
    """Aggregate recall@k on clean vs perturbed queries + the drop."""

    k: int
    clean_recall: float
    perturbed_recall: float
    drop_pp: float
    n_pairs: int

    def as_dict(self) -> dict:
        return {
            "k": self.k,
            "clean_recall": self.clean_recall,
            "perturbed_recall": self.perturbed_recall,
            "recall_drop_pp": self.drop_pp,
            "n_pairs": self.n_pairs,
        }


def recall_drop(pairs: Iterable[dict],
                retrieve_fn: Callable[[str], list[object]],
                gold_for: Callable[[object], Iterable[object]],
                k: int = 10) -> RecallDrop:
    """Mean recall@k drop over (clean, perturbed) query pairs.

    ``pairs`` items: ``{"clean": str, "variant": str, "parent_id": object}``.
    ``retrieve_fn`` maps a query -> ranked doc ids (live Qdrant in a real run, a
    stub in tests). ``gold_for`` maps a ``parent_id`` -> its gold doc ids. The
    retriever is injected so the arithmetic is unit-testable without an index.
    """
    pairs = list(pairs)
    clean_hits = perturbed_hits = 0.0
    for pair in pairs:
        gold = gold_for(pair["parent_id"])
        clean_hits += recall_at_k(retrieve_fn(pair["clean"]), gold, k)
        perturbed_hits += recall_at_k(retrieve_fn(pair["variant"]), gold, k)
    n = len(pairs)
    clean_recall = clean_hits / n if n else 0.0
    perturbed_recall = perturbed_hits / n if n else 0.0
    return RecallDrop(
        k=k,
        clean_recall=clean_recall,
        perturbed_recall=perturbed_recall,
        drop_pp=recall_drop_pp(clean_recall, perturbed_recall),
        n_pairs=n,
    )
