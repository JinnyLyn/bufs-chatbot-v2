"""Post-hoc answer translation.

Primary agent prompts are Korean, but this keeps a small fallback for cases where
the model still returns a predominantly non-Korean final answer to a Korean question.
"""

import logging
import re

from rag_agent.prompts import get_translation_prompt

logger = logging.getLogger(__name__)

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")


def needs_korean_translation(question: str, answer: str) -> bool:
    """True when the question is Korean but the answer is predominantly non-Korean."""
    if not question or not _HANGUL.search(question) or not answer:
        return False
    han = len(_HANGUL.findall(answer))
    lat = len(_LATIN.findall(answer))
    return lat > 0 and han < lat * 0.5


def to_korean(llm, answer: str, callbacks=None) -> str:
    """Translate `answer` to Korean via the shared LLM. Returns the original on failure.

    `callbacks` (e.g. the Langfuse handler) lets the translation LLM call appear in
    the same trace as the chat turn that triggered it.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        resp = llm.invoke(
            [SystemMessage(content=get_translation_prompt()), HumanMessage(content=answer)],
            config={"callbacks": callbacks} if callbacks else None,
        )
        return (resp.content or "").strip() or answer
    except Exception as exc:  # noqa: BLE001
        logger.warning("translation failed, keeping original: %s", exc)
        return answer
