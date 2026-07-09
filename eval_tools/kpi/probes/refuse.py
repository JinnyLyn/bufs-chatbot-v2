"""Fast-refuse robustness probe — over-refuse + under-refuse rates.

The deployed config runs with ``fast_refuse=true``: out-of-document questions
should be refused quickly ("제공된 자료에서 질문에 답할 수 있는 정보를 찾지
못했습니다" — the canonical refusal sentence shared by the orchestrator and
aggregation prompts). Two failure
modes matter on a *harder* boundary set than the clean 8-Q refusal check:

* **over-refuse** — an *answerable* question is wrongly refused (the bot bails on
  something it could have answered).
* **under-refuse** — an *out-of-document* question is wrongly answered (the bot
  hallucinates instead of refusing).

Refusal detection reuses the **canonical** :func:`eval_tools.kpi.scorer.is_refusal`
marker list (single source of truth — no re-hardcoded copy here). This module is
pure: the answer text is supplied (captured dump or injected live callable), the
refusal classification and rate arithmetic are offline + deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..scorer import is_refusal


@dataclass(frozen=True)
class RefuseMetrics:
    """Over/under-refuse counts + rates over a boundary set.

    ``answerable_total`` answerable items · ``over_refuse`` of those wrongly
    refused. ``unanswerable_total`` out-of-doc items · ``under_refuse`` of those
    wrongly answered (not refused).
    """

    answerable_total: int
    over_refuse: int
    unanswerable_total: int
    under_refuse: int

    @property
    def over_refuse_rate(self) -> Optional[float]:
        """Answerable questions wrongly refused / all answerable."""
        return self.over_refuse / self.answerable_total if self.answerable_total else None

    @property
    def under_refuse_rate(self) -> Optional[float]:
        """Out-of-doc questions wrongly answered / all out-of-doc."""
        return self.under_refuse / self.unanswerable_total if self.unanswerable_total else None

    def as_dict(self) -> dict:
        return {
            "answerable_total": self.answerable_total,
            "over_refuse": self.over_refuse,
            "over_refuse_rate": self.over_refuse_rate,
            "unanswerable_total": self.unanswerable_total,
            "under_refuse": self.under_refuse,
            "under_refuse_rate": self.under_refuse_rate,
        }


def _refused(item: dict) -> bool:
    """Whether an item counts as a refusal.

    Honours an explicit captured ``refused`` bool if present; otherwise derives
    it from the answer text via the canonical :func:`is_refusal`.
    """
    if "refused" in item and item["refused"] is not None:
        return bool(item["refused"])
    return is_refusal(str(item.get("answer") or ""))


def refuse_rates(items: Iterable[dict]) -> RefuseMetrics:
    """Compute over/under-refuse from items carrying ``answerable`` + answer/refusal.

    Each item: ``{"answerable": bool, "answer": str}`` (or a precomputed
    ``{"answerable": bool, "refused": bool}``).
    """
    a_tot = over = u_tot = under = 0
    for item in items:
        refused = _refused(item)
        if item.get("answerable"):
            a_tot += 1
            over += int(refused)            # answerable but refused -> over-refuse
        else:
            u_tot += 1
            under += int(not refused)       # out-of-doc but answered -> under-refuse
    return RefuseMetrics(
        answerable_total=a_tot, over_refuse=over,
        unanswerable_total=u_tot, under_refuse=under,
    )


def evaluate(items: Iterable[dict], answer_for: Callable[[dict], str]) -> RefuseMetrics:
    """Attach a live/captured answer to each item via ``answer_for`` then score.

    ``items`` carry at least ``answerable`` (+ ``question``); ``answer_for`` maps
    an item -> the model's answer string (live backend in a real run, stub in
    tests). Keeps the backend dependency outside the offline scoring path.
    """
    enriched = [{**item, "answer": answer_for(item)} for item in items]
    return refuse_rates(enriched)
