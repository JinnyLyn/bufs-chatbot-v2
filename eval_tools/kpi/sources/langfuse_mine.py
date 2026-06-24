"""Mine REAL user questions from Langfuse production traces (via the MCP).

The truest real-usage signal: what users actually typed in production. Pulled
via the Langfuse MCP (``fetch_traces`` / ``fetch_observations``), then
**PII-stripped**, deduped, intent-bucketed, and summarised in a coverage report.
Most production traces have **no ground_truth**, so they are scored via the judge
(RAGAS) path or a human-labelled subset — this module just surfaces and cleans
them; it does not score.

Connectivity contract (WS-R2)
-----------------------------
The MCP call cannot run *inside* this module (MCP tools belong to the agent /
CLI, not the Python process). So the fetch is **injected**: :func:`mine` takes a
``fetcher`` callable that returns raw trace dicts. If the fetcher raises or
yields nothing, :func:`mine` returns a :class:`MineSkipped` reason object —
**never a silent pass / empty success**. The pure half (:func:`process_traces`
and the PII/dedupe/bucket helpers) is fully offline-testable with synthetic
traces.

PII policy
----------
Strips emails, Korean phone numbers, 8–10-digit student-ID-like tokens, and
names adjacent to a role marker (``홍길동 학생`` -> ``[NAME] 학생``). Conservative
by design: better to leak a common noun past the filter than to mangle the
question — but every identifier shape in the suite's scope is covered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# PII stripping
# --------------------------------------------------------------------------- #

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # Korean mobile: 010-1234-5678 / 01012345678 / 010 1234 5678.
    ("[PHONE]", re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b")),
    # 학번 / generic 8–10 digit id (after phone, so it doesn't eat phone digits).
    ("[ID]", re.compile(r"\b\d{8,10}\b")),
    # A 2–4 syllable Korean name immediately before a role marker.
    ("[NAME]", re.compile(r"[가-힣]{2,4}(?=\s?(?:학생|교수|님|씨|선생|조교|학번))")),
)


def strip_pii(text: str) -> tuple[str, bool]:
    """Replace PII tokens with placeholders. Returns ``(clean_text, changed)``."""
    out = text
    for placeholder, pattern in _PII_PATTERNS:
        out = pattern.sub(placeholder, out)
    return out, out != text


# --------------------------------------------------------------------------- #
# Intent bucketing (reuses the combined88 taxonomy, priority-ordered)
# --------------------------------------------------------------------------- #

_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EARLY_GRADUATION", ("조기졸업",)),
    ("LEAVE_OF_ABSENCE", ("휴학", "복학")),
    ("MAJOR_CHANGE", ("전과", "복수전공", "부전공")),
    ("SCHOLARSHIP", ("장학", "장학금")),
    ("GRADUATION_REQ", ("졸업요건", "졸업", "이수학점", "졸업학점", "필수이수")),
    ("REGISTRATION", ("수강신청", "정정", "수강정정", "장바구니", "등록금", "재수강")),
    ("SCHEDULE", ("개강", "종강", "일정", "언제", "날짜", "시험", "방학", "기간")),
    ("CONTACT", ("연락처", "전화번호", "사무실", "문의", "위치")),
)


def bucket_intent(question: str) -> str:
    """Infer a coarse intent bucket from question keywords (``GENERAL`` fallback)."""
    for intent, keywords in _INTENT_KEYWORDS:
        if any(kw in question for kw in keywords):
            return intent
    return "GENERAL"


# --------------------------------------------------------------------------- #
# Trace -> (question, answer) extraction
# --------------------------------------------------------------------------- #


def _coerce_text(value: Any, keys: tuple[str, ...]) -> Optional[str]:
    """Pull a text field out of a str / dict / list trace input or output."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key]:
                return _coerce_text(value[key], keys)
        return None
    if isinstance(value, list) and value:
        # Common chat shape: take the last user/human-ish message content.
        return _coerce_text(value[-1], keys)
    return None


_QUESTION_KEYS = ("question", "input", "query", "content", "text", "message")
_ANSWER_KEYS = ("answer", "output", "content", "text", "response")


def extract_qa(trace: dict) -> tuple[Optional[str], Optional[str]]:
    """Best-effort ``(question, answer)`` from one trace's ``input``/``output``."""
    question = _coerce_text(trace.get("input"), _QUESTION_KEYS)
    if question is None:
        question = _coerce_text(trace.get("question"), _QUESTION_KEYS)
    answer = _coerce_text(trace.get("output"), _ANSWER_KEYS)
    if answer is None:
        answer = _coerce_text(trace.get("answer"), _ANSWER_KEYS)
    return question, answer


# --------------------------------------------------------------------------- #
# Result / skip objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MinedQuestion:
    """A cleaned, PII-stripped real-usage question (+ optional system answer)."""

    question: str
    answer: Optional[str]
    intent: str
    trace_id: Optional[str] = None
    pii_stripped: bool = False
    has_ground_truth: bool = False  # production traces almost never carry GT


@dataclass(frozen=True)
class CoverageReport:
    """Summary of what was mined (so the report never hides thin coverage)."""

    total_raw: int
    skipped_no_question: int
    total_unique: int
    pii_stripped: int
    with_answer: int
    without_ground_truth: int
    by_intent: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total_raw": self.total_raw,
            "skipped_no_question": self.skipped_no_question,
            "total_unique": self.total_unique,
            "pii_stripped": self.pii_stripped,
            "with_answer": self.with_answer,
            "without_ground_truth": self.without_ground_truth,
            "by_intent": dict(self.by_intent),
        }


@dataclass(frozen=True)
class MineResult:
    """Successful mine: cleaned questions + coverage. ``status == "ok"``."""

    questions: tuple[MinedQuestion, ...]
    coverage: CoverageReport
    status: str = "ok"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "coverage": self.coverage.as_dict(),
            "questions": [
                {
                    "question": q.question, "answer": q.answer, "intent": q.intent,
                    "trace_id": q.trace_id, "pii_stripped": q.pii_stripped,
                    "has_ground_truth": q.has_ground_truth,
                }
                for q in self.questions
            ],
        }


@dataclass(frozen=True)
class MineSkipped:
    """Mining could not run (MCP unreachable, no traces). ``status == "skipped"``.

    NEVER a silent pass: this is an explicit, logged reason the gate/report shows
    as ``real_usage(langfuse): SKIPPED — <reason>``.
    """

    reason: str
    detail: str = ""
    status: str = "skipped"

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "detail": self.detail}


# --------------------------------------------------------------------------- #
# Pure processing + injected-fetch entry point
# --------------------------------------------------------------------------- #


def process_traces(raw_traces: Iterable[dict]) -> MineResult:
    """Clean, PII-strip, dedupe, and intent-bucket raw trace dicts (pure)."""
    seen: set[str] = set()
    questions: list[MinedQuestion] = []
    total_raw = skipped = pii_count = with_answer = 0
    by_intent: dict[str, int] = {}

    for trace in raw_traces:
        total_raw += 1
        question, answer = extract_qa(trace)
        if not question:
            skipped += 1
            continue
        clean_q, q_changed = strip_pii(question)
        norm = re.sub(r"\s+", " ", clean_q).strip().lower()
        if norm in seen:
            continue
        seen.add(norm)
        clean_a, a_changed = (strip_pii(answer) if answer else (None, False))
        intent = bucket_intent(clean_q)
        by_intent[intent] = by_intent.get(intent, 0) + 1
        pii_changed = q_changed or a_changed
        pii_count += int(pii_changed)
        if clean_a:
            with_answer += 1
        questions.append(MinedQuestion(
            question=clean_q,
            answer=clean_a,
            intent=intent,
            trace_id=trace.get("id"),
            pii_stripped=pii_changed,
            has_ground_truth=False,
        ))

    coverage = CoverageReport(
        total_raw=total_raw,
        skipped_no_question=skipped,
        total_unique=len(questions),
        pii_stripped=pii_count,
        with_answer=with_answer,
        without_ground_truth=len(questions),  # none carry GT
        by_intent=dict(sorted(by_intent.items(), key=lambda kv: (-kv[1], kv[0]))),
    )
    return MineResult(questions=tuple(questions), coverage=coverage)


def mine(fetcher: Callable[[], Iterable[dict]]) -> MineResult | MineSkipped:
    """Fetch raw traces via the injected ``fetcher`` then process them.

    ``fetcher`` is a zero-arg callable returning raw trace dicts — in a live run
    it wraps the Langfuse MCP ``fetch_traces`` result; in tests it returns canned
    data. Any exception (MCP unreachable, auth/URL misconfig) -> a
    :class:`MineSkipped` with the reason; an empty result -> ``MineSkipped`` too.
    A successful fetch with usable questions -> :class:`MineResult`.
    """
    try:
        raw = list(fetcher())
    except Exception as exc:  # MCP unreachable / misconfigured / transport error
        return MineSkipped(reason="mcp_unreachable", detail=f"{type(exc).__name__}: {exc}")
    if not raw:
        return MineSkipped(reason="no_traces", detail="fetcher returned no traces")
    result = process_traces(raw)
    if not result.questions:
        return MineSkipped(
            reason="no_usable_questions",
            detail=f"{result.coverage.total_raw} traces, none had an extractable question",
        )
    return result
