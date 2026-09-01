"""Semester-aware retrieval scoping (issue: 학기 교차 오염).

The KB holds one 학사안내 per semester. Both describe the same *kinds* of facts
(개강일, 수강신청 기간, 성적 정정 기간 …) with different values, so a 2학기 question
retrieves 1학기 chunks that are lexically near-identical and factually wrong.

Measured on the sem2 100-Q set (qwen3.5:9b, 2026-07-29): for the 97 questions that
explicitly say "2학기", the retrieved source slots split 166 / 166 / 63 across
2학기 / 1학기 / semester-neutral documents — **half the retrieval budget is spent on
the wrong semester**, and three answers quoted 1학기 dates verbatim (ids 3, 17, 32).

This module supplies the pure decision logic; `tools.ToolFactory._search_child_chunks`
applies it behind ``config.SEMESTER_FILTER_ENABLED`` (default OFF).

Design decisions, each driven by a case that would otherwise regress:

1. **Demote, never delete.** Non-matching-semester chunks are pushed to the end of the
   candidate list rather than dropped. With a deep enough pool the wrong semester falls
   out of the top-k naturally, but a question whose evidence genuinely lives in the other
   semester's guide still finds it instead of hitting NO_RELEVANT_CHUNKS. (Qualified by
   #178: this holds for chunks clearing SEARCH_SCORE_THRESHOLD — wrong-semester chunks
   below it are excluded, as they already were by the old fetch-time cut. See
   ``select_semester_scoped``.)

2. **Semester-neutral documents always rank with the match.** 공인결석 매뉴얼, 등록금 안내,
   모바일 학생증 안내 … carry no semester and answer 63 of the measured source slots.
   Scoping must never push them behind the wrong semester.

3. **A semester marker naming a *different* 학년도 is ignored.** id=9 asks "2027학년도 1학기를
   위한 온라인 휴학 신청은 언제?" — the answer lives in the **2026학년도 2학기** guide, because a
   guide publishes the *next* semester's 휴복학 일정. Trusting the literal "1학기" here would
   demote the only document that can answer it. The year that decides is the one *attached
   to the marker*: "2025학년도 1학기에 휴학했는데 2026학년도에 복학…" must not pass this guard
   just because the current year appears somewhere else in the question.

4. **Both markers present → fall back to today's semester.** id=63 ("2학기에 … 1학기 레벨 1에서
   2로 …") names both; the question is asked *from* 2학기.

5. **"전기"/"후기" are NOT semester markers.** id=27 ("2026학년도 **전기** 학위수여식") means the
   February graduation ceremony, not 1학기. Matching on them mis-scoped a 2학기 question.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from rag_agent import scoping as _scoping

# "2학기", "2 학기", "제2학기" — the digit immediately qualifying 학기.
_SEM_IN_TEXT = re.compile(r"제?\s*([12])\s*학기")
# 학년도 / 학년 度 forms: "2026학년도", "2026 학년도", "2026년도"
_ACADEMIC_YEAR = re.compile(r"(20\d{2})\s*학?년\s*도")
# A 학년도 directly qualifying a semester marker: "2027학년도 1학기", "2026년도의 제2학기".
_YEAR_SEM_PAIR = re.compile(r"(20\d{2})\s*학?년\s*도의?\s*제?\s*([12])\s*학기")
# Filename forms: "2026학년도2학기학사안내", "2026-1 수강신청 매뉴얼", "2026년 1학기 …"
_SEM_IN_SOURCE = re.compile(r"([12])\s*학기")
_SEM_IN_SOURCE_DASH = re.compile(r"20\d{2}\s*-\s*([12])(?!\d)")


def academic_semester(today: Optional[_dt.date] = None) -> tuple[int, int]:
    """(학년도, 학기) in effect on ``today``.

    The Korean academic year starts in March. 2학기 is treated as starting in **August**,
    not September: 장바구니/수강신청 for 2학기 run through August (8/3 and 8/19 in the 2026
    calendar), so questions asked in August are already about 2학기. January–February fall
    in the *previous* 학년도's 2학기 (winter break).
    """
    today = today or _dt.date.today()
    y, m = today.year, today.month
    if 3 <= m <= 7:
        return y, 1
    if m >= 8:
        return y, 2
    return y - 1, 2


def target_semester(question: str, today: Optional[_dt.date] = None) -> int:
    """Which semester's documents this question is about (1 or 2).

    Never returns None: an unscoped question still gets today's semester, which is safe
    because semester-neutral documents are never demoted (see module docstring #2).
    """
    cur_year, cur_sem = academic_semester(today)
    found = {int(m) for m in _SEM_IN_TEXT.findall(question or "")}

    if len(found) != 1:
        # none, or both (#4) → the semester the asker is standing in
        return cur_sem

    marked = found.pop()
    # #3 keys on the 학년도 *attached to the marker*, not any year in the question: a bare
    # set-membership check would trust 2025's "1학기" in "2025학년도 1학기에 휴학했는데
    # 2026학년도에 복학…" merely because cur_year appears elsewhere.
    paired = {int(y) for y, s in _YEAR_SEM_PAIR.findall(question or "") if int(s) == marked}
    if paired:
        return marked if cur_year in paired else cur_sem
    years = {int(y) for y in _ACADEMIC_YEAR.findall(question or "")}
    if years and cur_year not in years:
        # unpaired marker, and every named 학년도 is a different one (#3) — the answering
        # guide is the current one
        return cur_sem
    return marked


def source_semester(source: str) -> Optional[int]:
    """Semester a KB source belongs to, or None when it is semester-neutral."""
    if not source:
        return None
    m = _SEM_IN_SOURCE.search(source) or _SEM_IN_SOURCE_DASH.search(source)
    return int(m.group(1)) if m else None


def is_wrong_semester(doc, target: int) -> bool:
    """True when the doc's source names a semester and it is not ``target``.

    Semester-neutral sources (no marker) are never wrong (module docstring #2).
    """
    src = (getattr(doc, "metadata", None) or {}).get("source", "")
    sem = source_semester(src)
    return sem is not None and sem != target


def select_semester_scoped(scored_docs: list, target: int, limit: int,
                           score_threshold: float) -> list:
    """Final selection over a deep pool fetched WITHOUT a score cutoff (#178).

    Applying the score threshold at fetch time shrank the pool to a handful of docs,
    so demotion had nothing left to promote. Here the threshold instead becomes part
    of the selection, preserving its original contract (exclude low-quality noise):

    - target/neutral docs that clear the threshold are kept, in retriever order;
    - wrong-semester docs must clear the threshold too, and only ever backfill
      (below-threshold wrong-semester docs never entered the pool on the old
      fetch-time cut either, so dropping them is not a behavior change);
    - a *sub-threshold* target/neutral doc is admitted only to stand in for a
      demoted wrong-semester doc — one admission per demotion. With no demotion
      there is no vacancy, so an off-topic question where nothing clears the
      threshold still returns [] and the NO_RELEVANT_CHUNKS → refusal routing
      (edges.py) keeps working exactly as it does with the lever OFF.

    ``scored_docs`` is a ranked list of ``(doc, score)`` pairs. The selection
    mechanics live in rag_agent.scoping so other levers (OCU 스코프) share them
    through a combined predicate.
    """
    return _scoping.select_scoped(
        scored_docs, lambda d: is_wrong_semester(d, target), limit, score_threshold)


def demote_other_semesters(docs: list, target: int) -> list:
    """Stable reorder: target-semester and semester-neutral docs first, others last.

    Order *within* each group is preserved, so the retriever's ranking still decides
    everything except the semester split.
    """
    return _scoping.demote_scoped(docs, lambda d: is_wrong_semester(d, target))
