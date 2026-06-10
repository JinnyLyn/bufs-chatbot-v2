"""Live LLM smoke integration test — requires a reachable Ollama instance.

Skipped automatically when OLLAMA_BASE_URL is not set (safe for offline CI).

Uses the `ollama` Python client directly (installed as a transitive dep of
langchain-ollama) because it supports `think=False` natively.  The langchain-
ollama 1.1.0 ChatOllama wrapper does not expose a `think` parameter; without
it the Qwen3.5 thinking model exhausts its token budget on hidden <think>
tokens and returns empty content.

Install extras before running:
    pip install langchain-ollama==1.1.0   # brings in ollama client

Run:
    OLLAMA_BASE_URL=http://127.0.0.1:11434 LLM_MODEL=qwen3.5:9b \\
        pytest tests/test_live_llm.py -v
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "")
_LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")

_SKIP_NO_OLLAMA = pytest.mark.skipif(
    not _OLLAMA_URL,
    reason="OLLAMA_BASE_URL not set — skipping live LLM tests",
)

# ollama is a direct dep of langchain-ollama — importorskip covers both
ollama = pytest.importorskip(
    "ollama",
    reason="ollama package not installed (pip install langchain-ollama==1.1.0)",
)


# ---------------------------------------------------------------------------
# Smoke: real ChatOllama invoke
# ---------------------------------------------------------------------------

@_SKIP_NO_OLLAMA
def test_live_ollama_returns_nonempty_response():
    """Ollama is reachable, model is loaded, and returns a non-empty response.

    Uses ollama.Client with think=False so Qwen3.5's hidden <think> tokens do
    not exhaust the prediction budget before actual content is generated.
    The langchain-ollama 1.1.0 ChatOllama wrapper does not expose a think
    parameter, so we use the underlying ollama client directly — it is the
    same SDK that ChatOllama delegates to.

    Timeout 60 s is generous for first-call VRAM load on RTX 4090.
    """
    client = ollama.Client(host=_OLLAMA_URL, timeout=60.0)
    response = client.chat(
        model=_LLM_MODEL,
        messages=[{"role": "user", "content": "안녕하세요. 한 문장으로 짧게 답해주세요."}],
        think=False,
        options={"temperature": 0, "num_predict": 128},
    )

    content = response.message.content
    assert isinstance(content, str), f"content is not str: {type(content)}"
    assert len(content.strip()) > 0, (
        f"Response content is empty. "
        f"done_reason={response.done_reason!r} "
        f"eval_count={response.eval_count}"
    )


@_SKIP_NO_OLLAMA
def test_live_ollama_responds_to_korean_query():
    """Model accepts a Korean factual query and returns a non-empty reply.

    think=False ensures token budget goes to the answer, not internal reasoning.
    """
    client = ollama.Client(host=_OLLAMA_URL, timeout=60.0)
    response = client.chat(
        model=_LLM_MODEL,
        messages=[{
            "role": "user",
            "content": "부산외국어대학교가 위치한 도시는 어디인가요? 한 단어로 답하세요.",
        }],
        think=False,
        options={"temperature": 0, "num_predict": 64},
    )
    content = response.message.content
    assert len(content.strip()) > 0, (
        f"No response to Korean query. done_reason={response.done_reason!r}"
    )


@_SKIP_NO_OLLAMA
def test_live_ollama_model_name_is_reachable():
    """Model name is present in Ollama's local model list (sanity / connectivity check)."""
    client = ollama.Client(host=_OLLAMA_URL, timeout=60.0)
    model_list = client.list()
    names = [m.model for m in model_list.models]
    assert any(_LLM_MODEL in name for name in names), (
        f"{_LLM_MODEL!r} not found in Ollama model list: {names}"
    )
