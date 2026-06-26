"""Canonical KPI scorer — the **corrected** scoring lineage, single source of truth.

Ported verbatim (logic-preserving) from ``eval_tools/_rescore88.py`` /
``eval_tools/_aggregate_variants.py`` — the *corrected* lineage, NOT the buggy
``_eval_combined88.py``. This module is PURE: stdlib + ``eval_tools.kpi.schema``
(also pure) only, no ``import config``, no file/network I/O. It runs in the
default offline ``pytest -m "not integration"`` lane.

CORRECTNESS (vs the buggy ``_eval_combined88.py`` lineage) — three deltas:
  D1  single-letter grades: ``(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])`` captures a
      bare ``A``/``B`` (combined88's ``[A-F]\\+`` misses them).
  D2  24h<->12h time: ``13<=n<=23`` matches ``오후 (n-12)시``.
  D3  **refusal-word subtraction is applied ONLY to unanswerable items.**
      Answerable items are judged PURELY on fact-presence — a correct answer
      that happens to contain a word like "불가능" is NOT dropped. (combined88
      subtracts ``is_refusal`` from answerable contains/strict — its largest
      bug, touching all 81 answerable items.)

Both lineages share the **empty-facts token-overlap fallback**: when
``extract_facts(gt)`` is empty, score on Korean/Latin token overlap
``ov = |gt∩ans| / |gt|`` with ``ov >= 0.6 -> full`` (strict) and
``ov >= 0.3 -> some`` (contains).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .schema import canonical_answer, canonical_ground_truth, is_answerable

# Refusal markers — a correct refusal must signal the info can't be found. Used
# ONLY for unanswerable items (answerable items are judged on fact-presence).
REFUSAL: tuple[str, ...] = (
    "없습니다", "없음", "불가", "확인할 수 없", "찾을 수 없", "포함되어 있지 않",
    "직접 확인", "명시되어 있지 않", "알 수 없", "제공되지 않", "찾지 못",
)

# Token pattern for the empty-facts overlap fallback (Korean syllables + Latin).
_TOKEN = re.compile(r"[가-힣A-Za-z]+")


def extract_facts(gt: str) -> set[str]:
    """Auto-extract key facts (dates, times, grades, bare numbers) from a GT string.

    Order matters (each pattern is consumed before the next): ``M월 D일`` and
    ``H:MM`` first, then ``M.D`` dates -> ``M월D일``, then single/▒-suffixed
    letter grades, then remaining bare numbers (4-digit years 19xx/20xx are
    skipped as question context, not answer facts).
    """
    facts: set[str] = set()
    s = gt
    for pat in (r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"):
        for m in re.findall(pat, s):
            facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split(".")
        facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])", s):  # grades incl. single letter
        facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n):  # skip cohort/years
            continue
        facts.add(n)
    return facts


def matched(fact: str, answer: str) -> bool:
    """Whether ``fact`` is present in ``answer`` under the corrected match rules.

    - bare number: digit-boundary match (so ``6`` ≠ ``16``); 24h hour ``13..23``
      also matches its 12h ``오후 (n-12)시`` form.
    - ``H:MM`` time: exact, zero-padded, and ``H시 MM분`` spoken forms.
    - ``M월D일`` date: substring after stripping spaces.
    - grade / token: plain substring.
    """
    a = answer
    if re.fullmatch(r"\d+", fact):
        n = int(fact)
        if re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")):
            return True
        if 13 <= n <= 23 and (f"오후 {n - 12}시" in a or f"오후{n - 12}시" in a):
            return True
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in (fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"))
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        return fact in a.replace(" ", "")
    return fact in a


def is_refusal(answer: str) -> bool:
    """True if the answer contains a refusal marker (unanswerable scoring only)."""
    return any(x in answer for x in REFUSAL)


@dataclass(frozen=True)
class ItemVerdict:
    """Per-question scoring verdict (the golden-corpus record shape).

    For unanswerable items, ``facts``/``matched`` are empty and ``full``/
    ``some`` are ``False``; the meaningful signal is ``is_refusal`` (a correct
    refusal == ``is_refusal is True``).
    """

    id: Any
    answerable: bool
    facts: list[str]
    matched: list[str]
    full: bool                  # strict: all facts present (or token ov >= 0.6)
    some: bool                  # contains: >=1 fact present (or token ov >= 0.3)
    is_refusal: bool
    used_token_fallback: bool


@dataclass(frozen=True)
class ScoreResult:
    """Aggregate scores + per-item verdicts.

    ``rates`` returns the ``(contains_rate, strict_rate, refusal_rate)`` triple
    matching ``_aggregate_variants.score()`` semantics.
    """

    contains_rate: float
    strict_rate: float
    refusal_rate: float
    answerable_total: int
    contains_count: int
    strict_count: int
    unanswerable_total: int
    refusal_count: int
    items: tuple[ItemVerdict, ...] = field(default_factory=tuple)

    @property
    def rates(self) -> tuple[float, float, float]:
        return (self.contains_rate, self.strict_rate, self.refusal_rate)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_item(record: dict) -> ItemVerdict:
    """Score a single record (canonical field access via :mod:`schema`)."""
    ans = canonical_answer(record)
    refused = is_refusal(ans)
    if is_answerable(record):
        gt = canonical_ground_truth(record)
        facts = extract_facts(gt)
        if facts:
            ordered = sorted(facts)
            hits = [f for f in ordered if matched(f, ans)]
            full = len(hits) == len(facts)
            some = len(hits) > 0
            used_fallback = False
        else:  # empty-facts token-overlap fallback
            gt_t = set(_TOKEN.findall(gt))
            a_t = set(_TOKEN.findall(ans))
            ov = len(gt_t & a_t) / max(1, len(gt_t))
            ordered, hits = [], []
            full, some = ov >= 0.6, ov >= 0.3
            used_fallback = True
        return ItemVerdict(
            id=record.get("id"), answerable=True, facts=ordered, matched=hits,
            full=full, some=some, is_refusal=refused, used_token_fallback=used_fallback,
        )
    # unanswerable: judged purely on correct refusal
    return ItemVerdict(
        id=record.get("id"), answerable=False, facts=[], matched=[],
        full=False, some=False, is_refusal=refused, used_token_fallback=False,
    )


def score(records: Iterable[dict]) -> ScoreResult:
    """Score a collection of records -> :class:`ScoreResult`.

    Semantics match ``_aggregate_variants.score()``: answerable items judged on
    fact-presence (``contains`` = ``some``, ``strict`` = ``full``), unanswerable
    items on correct refusal. Refusal-word subtraction is NEVER applied to
    answerable items (the D3 correction).
    """
    items: list[ItemVerdict] = []
    a_tot = contains = strict = 0
    r_tot = r_ok = 0
    for record in records:
        verdict = score_item(record)
        items.append(verdict)
        if verdict.answerable:
            a_tot += 1
            contains += int(verdict.some)
            strict += int(verdict.full)
        else:
            r_tot += 1
            r_ok += int(verdict.is_refusal)
    return ScoreResult(
        contains_rate=_rate(contains, a_tot),
        strict_rate=_rate(strict, a_tot),
        refusal_rate=_rate(r_ok, r_tot),
        answerable_total=a_tot,
        contains_count=contains,
        strict_count=strict,
        unanswerable_total=r_tot,
        refusal_count=r_ok,
        items=tuple(items),
    )
