"""``is_clear`` clarity-gate probe — confusion matrix + precision/recall.

The rewrite node emits ``QueryAnalysis.is_clear`` (``project/rag_agent/schemas.py``);
``route_after_rewrite`` (``project/rag_agent/edges.py``) routes ``is_clear=False``
to ``request_clarification`` and ``is_clear=True`` to ``agent``. On real, messy
input this gate misbehaves two ways:

* **false-clarify** — a genuinely *clear* question is wrongly sent to
  ``request_clarification`` (the user is nagged instead of answered).
* **false-answer** — a genuinely *ambiguous* question is wrongly sent to
  ``agent`` (the bot guesses instead of asking).

This module is the **pure** scoring half: given a labelled set with each item's
captured/live ``is_clear`` decision it computes the confusion matrix and
clarity precision/recall. The LLM that *produces* ``is_clear`` is injected (live
runner) or captured in a dump — never imported here — so the logic stays
offline-testable and ``config``-free.

Convention: the positive class is "clear" (``is_clear=True`` predicts *answer
directly*). So precision = "of the questions it answered, how many were truly
clear", recall = "of truly clear questions, how many it answered".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

CLEAR = "clear"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ClarityMetrics:
    """Confusion matrix + derived rates for the ``is_clear`` gate.

    ``tp`` truly-clear answered · ``fp`` ambiguous wrongly answered (false-answer)
    ``fn`` clear wrongly clarified (false-clarify) · ``tn`` ambiguous clarified.
    """

    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> Optional[float]:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> Optional[float]:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None

    @property
    def f1(self) -> Optional[float]:
        p, r = self.precision, self.recall
        if not p or not r:
            return None
        return 2 * p * r / (p + r)

    @property
    def false_clarify_rate(self) -> Optional[float]:
        """Clear questions wrongly clarified / all clear questions (FN / (TP+FN))."""
        denom = self.tp + self.fn
        return self.fn / denom if denom else None

    @property
    def false_answer_rate(self) -> Optional[float]:
        """Ambiguous questions wrongly answered / all ambiguous (FP / (FP+TN))."""
        denom = self.fp + self.tn
        return self.fp / denom if denom else None

    def as_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "total": self.total,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "false_clarify_rate": self.false_clarify_rate,
            "false_answer_rate": self.false_answer_rate,
        }


def _normalize_label(label: object) -> str:
    """Map a label to ``clear`` / ``ambiguous`` (accepts bools and common strings)."""
    if isinstance(label, bool):
        return CLEAR if label else AMBIGUOUS
    text = str(label).strip().lower()
    if text in {CLEAR, "true", "1", "yes", "answerable"}:
        return CLEAR
    if text in {AMBIGUOUS, "false", "0", "no", "unclear", "vague"}:
        return AMBIGUOUS
    raise ValueError(f"unrecognized clarity label {label!r}")


def confusion_matrix(items: Iterable[dict]) -> ClarityMetrics:
    """Build :class:`ClarityMetrics` from items carrying ``label`` + ``is_clear``.

    Each item: ``{"label": "clear"|"ambiguous"|bool, "is_clear": bool}``. ``label``
    is the gold annotation; ``is_clear`` is the gate's (captured/live) decision.
    """
    tp = fp = fn = tn = 0
    for item in items:
        label = _normalize_label(item["label"])
        is_clear = bool(item["is_clear"])
        if label == CLEAR:
            if is_clear:
                tp += 1          # clear, answered -> correct
            else:
                fn += 1          # clear, clarified -> false-clarify
        else:  # AMBIGUOUS
            if is_clear:
                fp += 1          # ambiguous, answered -> false-answer
            else:
                tn += 1          # ambiguous, clarified -> correct
    return ClarityMetrics(tp=tp, fp=fp, fn=fn, tn=tn)


def evaluate(labelled: Iterable[dict], decide_is_clear: Callable[[str], bool]) -> ClarityMetrics:
    """Run ``decide_is_clear`` over each labelled question, then score.

    ``labelled`` items: ``{"question": str, "label": "clear"|"ambiguous"}``.
    ``decide_is_clear`` maps a question -> the gate's ``is_clear`` bool; in a live
    run it wraps the rewrite node / LLM, in tests it is a stub. This keeps the
    LLM dependency entirely outside the (offline, ``config``-free) scoring path.
    """
    items = [
        {"label": row["label"], "is_clear": bool(decide_is_clear(row["question"]))}
        for row in labelled
    ]
    return confusion_matrix(items)
