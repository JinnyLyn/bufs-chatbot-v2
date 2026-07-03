"""RAGAS-style KPI runner — LLM-as-judge eval (opt-in).

Opt-in via ``--with-ragas``.  Requires a configured local Ollama judge
endpoint.  When no judge is configured:

  * Returns a :class:`RagasSentinel` N/A object (never a silent pass).
  * The gate treats RAGAS as **SKIPPED** (does not block the run).
  * With ``--require-ragas``, the caller should convert the sentinel to an
    ERROR (exit 2) — that policy lives in the gate, not here.

Five metrics (ported verbatim from ``eval_tools/_ragas_eval.py``):
  faithfulness, answer_relevancy, context_precision, context_recall,
  answer_correctness.

Integration
-----------
The live judge path (judge endpoint provided) is marked
``@pytest.mark.integration`` in the test suite — it makes actual LLM calls
and is deselected by the default ``pytest -m "not integration"`` filter.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional, Union


# ---------------------------------------------------------------------------
# RAGAS judge prompts (verbatim from _ragas_eval.py)
# ---------------------------------------------------------------------------

_FAITHFULNESS_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
생성된 답변이 오직 검색된 컨텍스트 내의 정보에만 근거하는지 평가합니다.
점수 기준: 1.0 모든 주장이 컨텍스트에 근거 / 0.8~0.9 대부분 근거(사소한 형식변환) / 0.5~0.7 일부 근거없음 / 0.0~0.3 핵심을 지어냄.
중요: "컨텍스트에 정보가 없어 문의 바랍니다"는 환각이 아닙니다(실제 없으면 1.0).
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

_ANSWER_RELEVANCY_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
답변이 질문 의도에 부합하는지 평가합니다. 핵심 정보(날짜/숫자/조건)를 정확히 포함하면 0.8 이상.
0.9~1.0 핵심 정확+한정어 반영 / 0.8 핵심 정확 / 0.6~0.7 부분 / 0.4~0.5 "문의하세요"만 / 0.0~0.3 무관.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

_CONTEXT_PRECISION_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
검색된 컨텍스트 중 질문에 답하는 데 유용한 정보의 비율을 평가합니다.
1.0 전부 관련 / 0.7~0.9 핵심+일부노이즈 / 0.4~0.6 반반 / 0.1~0.3 대부분 무관 / 0.0 전혀 무관.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

_CONTEXT_RECALL_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
정답(reference)을 도출할 근거가 컨텍스트에 포함되어 있는지 평가합니다. 표현이 달라도 같은 사실이면 포함.
0.9~1.0 모든 핵심 있음 / 0.8 핵심있음+세부누락 / 0.5~0.7 일부 / 0.2~0.4 부족 / 0.0 없음.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

_ANSWER_CORRECTNESS_SYSTEM = """당신은 RAG 시스템 평가 전문가입니다.
생성된 답변이 정답(reference)과 얼마나 일치하는지 평가합니다.
1.0 핵심(날짜/숫자/조건) 모두 일치 / 0.7~0.9 핵심맞고 세부누락 / 0.4~0.6 일부 / 0.1~0.3 대부분 불일치 / 0.0 완전 불일치.
반드시 JSON만: {"score": 0.0, "reason": "한 줄"}"""

# {metric_name: (system_prompt, user_template)}
_METRIC_CONFIG: dict[str, tuple[str, str]] = {
    "faithfulness": (
        _FAITHFULNESS_SYSTEM,
        "[검색된 컨텍스트]\n{context}\n\n[생성된 답변]\n{answer}\n\n답변이 컨텍스트에만 근거하는지 평가해 JSON으로.",
    ),
    "answer_relevancy": (
        _ANSWER_RELEVANCY_SYSTEM,
        "[질문]\n{question}\n\n[생성된 답변]\n{answer}\n\n답변이 질문 의도에 부합하는지 평가해 JSON으로.",
    ),
    "context_precision": (
        _CONTEXT_PRECISION_SYSTEM,
        "[질문]\n{question}\n\n[정답]\n{reference}\n\n[검색된 컨텍스트]\n{context}\n\n컨텍스트가 유용한지 평가해 JSON으로.",
    ),
    "context_recall": (
        _CONTEXT_RECALL_SYSTEM,
        "[정답]\n{reference}\n\n[검색된 컨텍스트]\n{context}\n\n정답 근거가 컨텍스트에 있는지 평가해 JSON으로.",
    ),
    "answer_correctness": (
        _ANSWER_CORRECTNESS_SYSTEM,
        "[질문]\n{question}\n\n[정답]\n{reference}\n\n[생성된 답변]\n{answer}\n\n답변이 정답과 일치하는지 평가해 JSON으로.",
    ),
}

METRIC_NAMES: tuple[str, ...] = tuple(_METRIC_CONFIG)

# Judge-input truncation limits — single source of truth so ad-hoc judges
# (eval_tools/_error_analysis7.py --judge) and this runner score on the SAME
# input sizes and stay comparable (PR #79 review). Unified 2026-07-03 to the
# larger limits: faithfulness needs enough context to check groundedness.
JUDGE_QUESTION_CHARS = 500
JUDGE_CONTEXT_CHARS = 6000
JUDGE_ANSWER_CHARS = 4000
JUDGE_REFERENCE_CHARS = 300

# Sentinel value for missing/failed scores
_NA_STR = "N/A — no judge endpoint"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RagasSentinel:
    """Returned when RAGAS cannot run (no judge endpoint configured).

    ``is_na`` is always ``True``.  The gate records each metric as N/A and
    treats the RAGAS family as **SKIPPED** — NOT a silent pass, NOT a NO-GO
    (unless ``--require-ragas`` was set, in which case the gate converts this
    to an ERROR).
    """

    reason: str
    metrics: dict[str, str] = field(default_factory=dict)  # {metric: "N/A — ..."}

    @property
    def is_na(self) -> bool:
        return True


@dataclass(frozen=True)
class RagasResult:
    """RAGAS scores from a successful live judge run.

    ``metrics`` maps each metric name to its float score (or ``-1.0`` for
    a failed judge call on that metric).  ``avg`` is the mean of valid
    (>= 0) scores.
    """

    metrics: dict[str, float]
    judge_model: str
    n: int            # number of records judged

    @property
    def is_na(self) -> bool:
        return False

    @property
    def avg(self) -> Optional[float]:
        valid = [v for v in self.metrics.values() if v >= 0]
        return round(sum(valid) / len(valid), 4) if valid else None


# ---------------------------------------------------------------------------
# Internal judge helpers
# ---------------------------------------------------------------------------

def _extract_score(text: str) -> tuple[float, str]:
    """Parse a JSON ``{"score": ..., "reason": ...}`` fragment from judge output.

    Tries, in order: whole-text ``json.loads`` -> greedy ``{...}`` block (handles
    braces inside the reason string) -> narrow brace-free block (historical
    behavior). Any parse failure yields ``(-1.0, "")`` — the caller treats -1 as
    N/A, never as a score.
    """
    candidates = [text or ""]
    m = re.search(r"\{.*\}", text or "", re.DOTALL)   # greedy: first { .. last }
    if m:
        candidates.append(m.group())
    m = re.search(r"\{[^{}]*\}", text or "", re.DOTALL)
    if m:
        candidates.append(m.group())
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        # "score" 키가 없으면 파싱 실패로 취급 — 과거엔 기본값 -1이 0.0으로 클램프되어
        # N/A가 "완전 불일치 0점"으로 둔갑하는 무음 오류였다.
        if not isinstance(obj, dict) or "score" not in obj:
            continue
        try:
            raw = float(obj["score"])
        except (TypeError, ValueError):
            continue
        return max(0.0, min(1.0, raw)), str(obj.get("reason", ""))
    return -1.0, ""


def _ollama_judge(system: str, prompt: str, *, url: str, model: str) -> str:
    """Call a local Ollama judge endpoint.

    Thinking models (qwen3.5 등) can burn the whole ``num_predict`` budget in the
    ``thinking`` channel and return an **empty** ``content``. First call is verbatim
    (non-thinking judges keep the historical request shape); on empty content we
    retry once with ``think: false``. If the retry itself fails (e.g. HTTP 400 from
    a model that rejects the ``think`` param), we fall back to the first call's
    empty content — the caller's ``_extract_score`` then yields ``-1`` (N/A), which
    is the pre-patch behavior; we never escalate the retry into a crash.
    """
    import requests  # lazy — only in live path

    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 256},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    # 1st attempt: verbatim request (historical shape). Errors propagate — the
    # caller handles judge failure explicitly; swallowing them here would turn an
    # unreachable endpoint into a silent all--1 run.
    resp = requests.post(f"{url.rstrip('/')}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    content = (resp.json().get("message") or {}).get("content", "").strip()
    if content:
        return content
    # Empty content — likely a thinking model. Best-effort retry with think:false;
    # any failure here returns the empty content instead of raising.
    try:
        resp = requests.post(f"{url.rstrip('/')}/api/chat", json={**payload, "think": False}, timeout=120)
        resp.raise_for_status()
        return (resp.json().get("message") or {}).get("content", "").strip()
    except requests.exceptions.RequestException:
        return content


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    records: list[dict],
    *,
    judge_url: Optional[str] = None,
    judge_model: Optional[str] = None,
    n: Optional[int] = None,
) -> Union[RagasSentinel, RagasResult]:
    """Run RAGAS eval or return a N/A sentinel if no judge is configured.

    Parameters
    ----------
    records:
        Prediction-dump records.  Each needs ``question``, ``ground_truth``,
        ``answer``, and optionally ``results`` (for context).
    judge_url:
        Local Ollama base URL (e.g. ``http://localhost:11434``) or ``None``.
    judge_model:
        Judge model name.  ``None`` → return :class:`RagasSentinel`.
    n:
        Limit number of records to judge.  ``None`` → judge all.

    Returns
    -------
    RagasSentinel
        When no judge endpoint/model is configured.
    RagasResult
        When a judge is configured and calls succeed.
    """
    # ---- No judge configured: return N/A sentinel immediately ----
    if not (bool(judge_model) and bool(judge_url)):
        reason = (
            "no judge endpoint configured"
            if not judge_model
            else "judge_model set but no judge URL provided"
        )
        return RagasSentinel(
            reason=reason,
            metrics={m: _NA_STR for m in METRIC_NAMES},
        )

    # ---- Live judge path (integration-only) ----
    answerable = [r for r in records if r.get("answerable", True)]
    if n is not None:
        answerable = answerable[:n]

    def _context(r: dict) -> str:
        results = r.get("results") or []
        if isinstance(results, list):
            return "\n\n".join(str(x.get("text", "")) for x in results if isinstance(x, dict))
        return ""

    def _call_judge(system: str, prompt: str) -> str:
        return _ollama_judge(system, prompt, url=judge_url or "", model=judge_model or "")

    agg: dict[str, list[float]] = {m: [] for m in METRIC_NAMES}

    for i, r in enumerate(answerable):
        q = r.get("question", "")
        ref = r.get("ground_truth", "")
        ans = r.get("answer", "")
        ctx = _context(r)

        for metric in METRIC_NAMES:
            system, tmpl = _METRIC_CONFIG[metric]
            prompt = tmpl.format(
                question=q[:JUDGE_QUESTION_CHARS],
                context=ctx[:JUDGE_CONTEXT_CHARS],
                answer=ans[:JUDGE_ANSWER_CHARS],
                reference=ref[:JUDGE_REFERENCE_CHARS],
            )
            try:
                sc, _ = _extract_score(_call_judge(system, prompt))
            except Exception as e:  # noqa: BLE001 — 실패는 -1(N/A)로 집계하되 반드시 로그
                print(f"[warn] RAGAS judge 호출 실패 (record id={r.get('id')}, metric={metric}): "
                      f"{type(e).__name__}: {e}")
                sc = -1.0

            agg[metric].append(sc)

    metrics_out = {
        m: round(sum(v for v in vals if v >= 0) / max(1, sum(1 for v in vals if v >= 0)), 4)
        if any(v >= 0 for v in vals) else -1.0
        for m, vals in agg.items()
    }

    return RagasResult(
        metrics=metrics_out,
        judge_model=judge_model or "",
        n=len(answerable),
    )
