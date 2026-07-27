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


class TestCentralizedMicroPrompts:
    """Micro-prompts formerly hardcoded in translate.py / nodes.py — moved to
    prompts.py verbatim so the whole prompt surface lives in one file. Texts
    must stay byte-identical to the original inline versions (behavior-neutral)."""

    def test_translation_prompt(self):
        p = _import_prompts()
        text = p.get_translation_prompt()
        assert "번역" in text and "숫자" in text and "파일명" in text

    def test_force_search_instruction(self):
        p = _import_prompts()
        assert "search_child_chunks" in p.get_force_search_instruction()

    def test_fallback_task_instruction(self):
        p = _import_prompts()
        assert len(p.get_fallback_task_instruction()) > 0
