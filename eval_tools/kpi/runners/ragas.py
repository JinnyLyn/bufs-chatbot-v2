"""RAGAS-style KPI runner — LLM-as-judge eval (opt-in).

Opt-in via ``--with-ragas``.  Requires a configured judge endpoint (local
Ollama or Gemini REST).  When no judge is configured:

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
import time
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
    judge_type: str   # "ollama" | "gemini"
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
    """Parse a JSON ``{"score": ..., "reason": ...}`` fragment from judge output."""
    m = re.search(r"\{[^{}]*\}", text or "", re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group())
            score = max(0.0, min(1.0, float(obj.get("score", -1))))
            return score, obj.get("reason", "")
        except (ValueError, KeyError):
            pass
    return -1.0, ""


def _ollama_judge(system: str, prompt: str, *, url: str, model: str) -> str:
    """Call a local Ollama judge endpoint."""
    import requests  # lazy — only in live path

    resp = requests.post(
        f"{url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 256},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


def _gemini_judge(
    system: str,
    prompt: str,
    *,
    api_key: str,
    model: str,
    ca_bundle: Optional[str] = None,
    inter_call_delay: float = 4.0,
) -> str:
    """Call Gemini REST judge with retry on 429."""
    import requests  # lazy — only in live path

    # Key goes in the x-goog-api-key header, NOT the URL query string — a URL
    # key leaks into access/proxy logs and request traces (S1).
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 256,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    kwargs: dict = {
        "json": payload,
        "timeout": 60,
        "headers": {"x-goog-api-key": api_key},
    }
    if ca_bundle:
        kwargs["verify"] = ca_bundle

    for attempt in range(5):
        resp = requests.post(url, **kwargs)
        if resp.status_code == 200:
            d = resp.json()
            try:
                return "".join(
                    p.get("text", "")
                    for p in d["candidates"][0]["content"]["parts"]
                ).strip()
            except (KeyError, IndexError):
                return ""
        if resp.status_code == 429 and attempt < 4:
            backoff = min(15 * (2 ** attempt), 90)
            # Honor the server's Retry-After when present (E4): it knows the
            # quota window better than a fixed exponential backoff.
            try:
                backoff = int(resp.headers.get("Retry-After", backoff))
            except (TypeError, ValueError):
                pass
            time.sleep(backoff)
            continue
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:160]}")
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    records: list[dict],
    *,
    judge_url: Optional[str] = None,
    judge_model: Optional[str] = None,
    judge_type: str = "ollama",  # "ollama" | "gemini"
    gemini_api_key: Optional[str] = None,
    gemini_ca_bundle: Optional[str] = None,
    inter_call_delay: float = 4.0,
    n: Optional[int] = None,
) -> Union[RagasSentinel, RagasResult]:
    """Run RAGAS eval or return a N/A sentinel if no judge is configured.

    Parameters
    ----------
    records:
        Prediction-dump records.  Each needs ``question``, ``ground_truth``,
        ``answer``, and optionally ``results`` (for context).
    judge_url:
        Ollama base URL (e.g. ``http://localhost:11434``) or ``None``.
        For Gemini, pass ``None`` — authentication goes via ``gemini_api_key``.
    judge_model:
        Judge model name.  ``None`` → return :class:`RagasSentinel`.
    judge_type:
        ``"ollama"`` (default) or ``"gemini"``.
    gemini_api_key:
        Google API key (Gemini only).
    gemini_ca_bundle:
        Path to CA bundle for Gemini HTTPS (optional, for corporate proxies).
    inter_call_delay:
        Seconds to sleep between Gemini judge calls to avoid 429.
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
    needs_judge = bool(judge_model) and (
        judge_type == "gemini" and bool(gemini_api_key)
        or judge_type == "ollama" and bool(judge_url)
    )
    if not needs_judge:
        reason = (
            "no judge endpoint configured"
            if not judge_model
            else f"judge_model set but no {'API key' if judge_type == 'gemini' else 'URL'} provided"
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
        if judge_type == "gemini":
            return _gemini_judge(
                system, prompt,
                api_key=gemini_api_key or "",
                model=judge_model or "",
                ca_bundle=gemini_ca_bundle,
                inter_call_delay=inter_call_delay,
            )
        return _ollama_judge(system, prompt, url=judge_url or "", model=judge_model or "")

    agg: dict[str, list[float]] = {m: [] for m in METRIC_NAMES}

    for i, r in enumerate(answerable):
        q = r.get("question", "")
        ref = r.get("ground_truth", "")
        ans = r.get("answer", "")
        ctx = _context(r)

        for j, metric in enumerate(METRIC_NAMES):
            system, tmpl = _METRIC_CONFIG[metric]
            prompt = tmpl.format(
                question=q[:500],
                context=ctx[:1200],
                answer=ans[:500],
                reference=ref[:300],
            )
            try:
                sc, _ = _extract_score(_call_judge(system, prompt))
            except Exception:
                sc = -1.0

            agg[metric].append(sc)

            # Rate-limit between Gemini calls (not after last metric)
            if judge_type == "gemini" and j < len(METRIC_NAMES) - 1:
                time.sleep(inter_call_delay)

    metrics_out = {
        m: round(sum(v for v in vals if v >= 0) / max(1, sum(1 for v in vals if v >= 0)), 4)
        if any(v >= 0 for v in vals) else -1.0
        for m, vals in agg.items()
    }

    return RagasResult(
        metrics=metrics_out,
        judge_model=judge_model or "",
        judge_type=judge_type,
        n=len(answerable),
    )
