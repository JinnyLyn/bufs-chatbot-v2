"""Post-hoc answer translation.

Primary agent prompts are Korean, but this keeps a small fallback for cases where
the model still returns a predominantly non-Korean final answer to a Korean question.
"""

import logging
import re

logger = logging.getLogger(__name__)

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")

_TRANSLATE_SYSTEM = (
    "당신은 전문 번역가입니다. 사용자의 메시지를 자연스럽고 유창한 한국어로 번역하세요. "
    "한국어 번역문만 출력하고, 머리말, 주석, 따옴표를 붙이지 마세요. "
    "Markdown 형식, 숫자, 학점 수, URL, 문서명, 파일명은 그대로 보존하세요."
)


def needs_korean_translation(question: str, answer: str) -> bool:
    """True when the question is Korean but the answer is predominantly non-Korean."""
    if not question or not _HANGUL.search(question) or not answer:
        return False
    han = len(_HANGUL.findall(answer))
    lat = len(_LATIN.findall(answer))
    return lat > 0 and han < lat * 0.5


def to_korean(llm, answer: str) -> str:
    """Translate `answer` to Korean via the shared LLM. Returns the original on failure."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        resp = llm.invoke([SystemMessage(content=_TRANSLATE_SYSTEM), HumanMessage(content=answer)])
        return (resp.content or "").strip() or answer
    except Exception as exc:  # noqa: BLE001
        logger.warning("translation failed, keeping original: %s", exc)
        return answer
