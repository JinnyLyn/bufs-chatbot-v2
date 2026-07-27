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


class TestUserSlotsTypeTolerance:
    """A single model-authored type mismatch must not discard the whole extraction.

    Regression: live 2026-07-27 (qwen3.5:9b) returned extra="" for "이번 학기 18학점
    신청했는데 21학점까지 …" — the ValidationError threw away the correctly-extracted
    credits/semester with it and the question silently fell back to no-slots.
    """

    def _slots(self, **kwargs):
        from rag_agent.schemas import UserSlots
        return UserSlots(**kwargs)

    def test_empty_string_for_list_field_becomes_empty_list(self):
        s = self._slots(credits="18학점 신청", extra="")
        assert s.extra == []
        assert s.credits == "18학점 신청"   # the sibling slot survives

    def test_none_for_list_field_becomes_empty_list(self):
        assert self._slots(required_conditions=None).required_conditions == []

    def test_bare_string_for_list_field_becomes_one_item(self):
        assert self._slots(extra="등록금 일부만 납부").extra == ["등록금 일부만 납부"]

    def test_list_items_stringified_and_blanks_dropped(self):
        assert self._slots(extra=["  조건  ", "", None, 2024]).extra == ["조건", "2024"]

    def test_number_for_scalar_field_becomes_text(self):
        assert self._slots(credits=18).credits == "18"

    def test_none_for_scalar_field_becomes_empty_string(self):
        assert self._slots(major=None).major == ""

    def test_list_for_scalar_field_is_joined(self):
        assert self._slots(status=["휴학 중", "복학 예정"]).status == "휴학 중, 복학 예정"

    def test_defaults_still_empty(self):
        s = self._slots()
        assert s.extra == [] and s.required_conditions == [] and s.admission_year == ""
