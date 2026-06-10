"""Unit tests for rag_agent/schemas.py — Pydantic model validation."""
import pytest
from pydantic import ValidationError


def _import_schemas():
    from rag_agent.schemas import QueryAnalysis
    return QueryAnalysis


class TestQueryAnalysis:
    def test_valid_schema_instantiates(self):
        QueryAnalysis = _import_schemas()
        qa = QueryAnalysis(
            is_clear=True,
            questions=["부산외대 졸업학점은 몇 학점인가요?"],
            clarification_needed="",
        )
        assert qa.is_clear is True
        assert len(qa.questions) == 1

    def test_is_clear_false_with_clarification(self):
        QueryAnalysis = _import_schemas()
        qa = QueryAnalysis(
            is_clear=False,
            questions=[],
            clarification_needed="어느 학번 기준으로 알고 싶으신가요?",
        )
        assert qa.is_clear is False
        assert qa.clarification_needed != ""

    def test_questions_can_hold_multiple_items(self):
        QueryAnalysis = _import_schemas()
        qa = QueryAnalysis(
            is_clear=True,
            questions=["질문1", "질문2", "질문3"],
            clarification_needed="",
        )
        assert len(qa.questions) == 3

    def test_missing_required_field_raises_validation_error(self):
        QueryAnalysis = _import_schemas()
        with pytest.raises(ValidationError):
            QueryAnalysis(is_clear=True, questions=["q"])  # clarification_needed missing

    def test_questions_empty_list_is_valid(self):
        """Empty questions list is structurally valid (e.g. unclear query)."""
        QueryAnalysis = _import_schemas()
        qa = QueryAnalysis(is_clear=False, questions=[], clarification_needed="?")
        assert qa.questions == []

    def test_schema_serialises_to_dict(self):
        QueryAnalysis = _import_schemas()
        qa = QueryAnalysis(is_clear=True, questions=["q1"], clarification_needed="")
        d = qa.model_dump()
        assert "is_clear" in d
        assert "questions" in d
        assert "clarification_needed" in d
