"""Unit tests for rag_agent/prompts.py — prompt template accessors.

Each getter must return a non-empty Korean string. Tests confirm:
- Function is callable and returns str
- Output contains expected Korean domain-specific anchors
- No placeholder tokens left un-substituted (no curly braces for Python format)
"""
import pytest


def _import_prompts():
    from rag_agent import prompts
    return prompts


class TestConversationSummaryPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_conversation_summary_prompt(), str)

    def test_not_empty(self):
        p = _import_prompts()
        assert len(p.get_conversation_summary_prompt()) > 0

    def test_contains_summary_keyword(self):
        p = _import_prompts()
        text = p.get_conversation_summary_prompt()
        assert "요약" in text

    def test_no_unformatted_placeholders(self):
        p = _import_prompts()
        text = p.get_conversation_summary_prompt()
        # No Python-format placeholders like {variable} should remain
        import re
        assert not re.search(r"\{[a-z_]+\}", text)


class TestRewriteQueryPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_rewrite_query_prompt(), str)

    def test_contains_is_clear_field_reference(self):
        """Prompt should mention is_clear (the schema field name)."""
        p = _import_prompts()
        assert "is_clear" in p.get_rewrite_query_prompt()

    def test_contains_questions_field_reference(self):
        p = _import_prompts()
        assert "questions" in p.get_rewrite_query_prompt()

    def test_mentions_current_query_input(self):
        p = _import_prompts()
        assert "current_query" in p.get_rewrite_query_prompt()


class TestOrchestratorPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_orchestrator_prompt(), str)

    def test_mentions_search_child_chunks_tool(self):
        p = _import_prompts()
        assert "search_child_chunks" in p.get_orchestrator_prompt()

    def test_mentions_retrieve_parent_chunks_tool(self):
        p = _import_prompts()
        assert "retrieve_parent_chunks" in p.get_orchestrator_prompt()

    def test_contains_korean_language_instruction(self):
        p = _import_prompts()
        assert "한국어" in p.get_orchestrator_prompt()

    def test_refusal_is_last_resort_with_recheck(self):
        """Issue #81: 11/79 wrong answers were false refusals with the fact IN
        context — refusing must require re-reading every context block first."""
        p = _import_prompts()
        text = p.get_orchestrator_prompt()
        assert "마지막 수단" in text
        assert "직접 인용" in text

    def test_query_anchoring_rule(self):
        """Issue #87: 16/19 term-drift cases are dense-leg — the tool-call query
        itself must keep the original question's key nouns (anchoring, NOT the
        net-negative H1 full-passthrough: adding synonyms stays allowed)."""
        p = _import_prompts()
        text = p.get_orchestrator_prompt()
        assert "핵심 명사" in text
        assert "그대로 포함" in text

    def test_search_limit_10(self):
        """Issue #80 cause C: limit=5 hard-cut dropped rank-6~10 facts."""
        p = _import_prompts()
        assert "limit은 10" in p.get_orchestrator_prompt()

    def test_question_scope_rule(self):
        """Issue #81 id=36: leave-of-absence answer leaked 복학/분할납부 content."""
        p = _import_prompts()
        assert "섞지 마세요" in p.get_orchestrator_prompt()

    def test_canonical_refusal_sentence_shared_with_aggregation(self):
        """Single canonical refusal sentence across orchestrator + aggregation
        (was two different hardcoded sentences)."""
        p = _import_prompts()
        sentence = "제공된 자료에서 질문에 답할 수 있는 정보를 찾지 못했습니다."
        assert sentence in p.get_orchestrator_prompt()
        assert sentence in p.get_aggregation_prompt()


class TestFallbackResponsePrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_fallback_response_prompt(), str)

    def test_not_empty(self):
        p = _import_prompts()
        assert len(p.get_fallback_response_prompt()) > 0

    def test_mentions_sources_section(self):
        p = _import_prompts()
        assert "출처" in p.get_fallback_response_prompt()


class TestContextCompressionPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_context_compression_prompt(), str)

    def test_mentions_compression_task(self):
        p = _import_prompts()
        assert "압축" in p.get_context_compression_prompt()

    def test_preserves_question_critical_facts(self):
        """Issue #81: both compression-loss wrong answers dropped the asked-for
        fact (date/period/department) from the summary."""
        p = _import_prompts()
        text = p.get_context_compression_prompt()
        assert "기간" in text
        assert "부서" in text
        assert "생략하지 마세요" in text


class TestAggregationPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_aggregation_prompt(), str)

    def test_contains_korean_language_rule(self):
        p = _import_prompts()
        assert "한국어" in p.get_aggregation_prompt()

    def test_mentions_source_file_extension_rule(self):
        """Must include the file-extension filtering rule to avoid chunk ID leakage."""
        p = _import_prompts()
        text = p.get_aggregation_prompt()
        assert ".pdf" in text or "확장자" in text


class TestTranslationPrompt:
    def test_returns_string(self):
        p = _import_prompts()
        assert isinstance(p.get_translation_prompt(), str)

    def test_preserves_numbers_and_filenames(self):
        p = _import_prompts()
        text = p.get_translation_prompt()
        assert "숫자" in text and "파일명" in text


class TestInlineInstructions:
    """Micro-prompts formerly hardcoded inline in nodes.py — centralized here so
    prompts.py shows the full prompt surface."""

    def test_force_search_instruction(self):
        p = _import_prompts()
        text = p.get_force_search_instruction()
        assert isinstance(text, str)
        assert "search_child_chunks" in text

    def test_fallback_task_instruction(self):
        p = _import_prompts()
        text = p.get_fallback_task_instruction()
        assert isinstance(text, str)
        assert len(text) > 0
