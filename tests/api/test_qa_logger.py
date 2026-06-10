"""Unit tests for api/qa_logger.py — JSONL record shape, file naming, skip flags.

All tests use tmp_path so they never touch the real logs/ directory.
"""
import importlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


def _get_qa_logger_module():
    """Fresh import of qa_logger (reloads config too to pick up env changes)."""
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    if "api.qa_logger" in sys.modules:
        importlib.reload(sys.modules["api.qa_logger"])
    import api.qa_logger as m
    return m


# ---------------------------------------------------------------------------
# QALogger — basic write / read round-trip
# ---------------------------------------------------------------------------

class TestQALoggerRecordShape:
    def test_log_writes_jsonl_file(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="졸업학점은?", answer="130학점")
        files = list(tmp_path.glob("qa_*.jsonl"))
        assert len(files) == 1

    def test_log_file_named_with_today_date(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q", answer="a")
        today = date.today().isoformat()
        assert (tmp_path / f"qa_{today}.jsonl").exists()

    def test_log_record_contains_required_fields(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="질문", answer="답변", trace_id="abc12345", session_id="s1")
        records = logger.read()
        assert len(records) == 1
        rec = records[0]
        for field in ("timestamp", "trace_id", "session_id", "question", "answer",
                      "duration_ms", "num_results", "sources", "sub_questions", "tool_calls", "timing"):
            assert field in rec, f"missing field: {field}"

    def test_log_record_question_and_answer_preserved(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="세계는 어떻게 됐나", answer="잘 모르겠습니다")
        rec = logger.read()[0]
        assert rec["question"] == "세계는 어떻게 됐나"
        assert rec["answer"] == "잘 모르겠습니다"

    def test_multiple_logs_appended_to_same_file(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q1", answer="a1")
        logger.log(question="q2", answer="a2")
        records = logger.read()
        assert len(records) == 2

    def test_sources_defaults_to_empty_list(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q", answer="a")
        assert logger.read()[0]["sources"] == []

    def test_sources_list_stored_correctly(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q", answer="a", sources=["doc1.pdf", "doc2.pdf"])
        assert logger.read()[0]["sources"] == ["doc1.pdf", "doc2.pdf"]

    def test_timing_dict_stored_correctly(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        timing = {"rewrite_ms": 120, "search_ms": 340}
        logger.log(question="q", answer="a", timing=timing)
        assert logger.read()[0]["timing"] == timing


# ---------------------------------------------------------------------------
# Skip flags — CHAT_LOG_DISABLED and ContextVar _skip_var
# ---------------------------------------------------------------------------

class TestSkipFlags:
    def test_chat_log_disabled_env_prevents_write(self, tmp_path, env_isolated, monkeypatch):
        """When CHAT_LOG_DISABLED=1, log() must write nothing."""
        monkeypatch.setenv("CHAT_LOG_DISABLED", "1")
        m = _get_qa_logger_module()
        assert m.should_skip_log() is True
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q", answer="a")
        assert logger.read() == []

    def test_skip_var_contextvar_prevents_write(self, tmp_path, env_isolated):
        m = _get_qa_logger_module()
        m.set_skip_log(True)
        try:
            logger = m.QALogger(log_dir=tmp_path)
            logger.log(question="q", answer="a")
            assert logger.read() == []
        finally:
            m.set_skip_log(False)

    def test_skip_false_allows_write(self, tmp_path, env_isolated):
        m = _get_qa_logger_module()
        m.set_skip_log(False)
        logger = m.QALogger(log_dir=tmp_path)
        logger.log(question="q", answer="a")
        assert len(logger.read()) == 1


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

class TestReadHelpers:
    def test_read_with_specific_date(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        # Write a record for "yesterday"
        yesterday = date.today() - timedelta(days=1)
        path = tmp_path / f"qa_{yesterday.isoformat()}.jsonl"
        entry = {"timestamp": "2026-01-01T00:00:00", "question": "어제 질문", "answer": "a"}
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        records = logger.read(d=yesterday)
        assert len(records) == 1
        assert records[0]["question"] == "어제 질문"

    def test_read_all_aggregates_across_dates(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        for i in range(3):
            d = date.today() - timedelta(days=i)
            p = tmp_path / f"qa_{d.isoformat()}.jsonl"
            p.write_text(json.dumps({"q": str(i)}) + "\n", encoding="utf-8")
        records = logger.read_all()
        assert len(records) == 3

    def test_list_dates_returns_sorted_dates(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        for i in range(3):
            d = date.today() - timedelta(days=i)
            p = tmp_path / f"qa_{d.isoformat()}.jsonl"
            p.write_text("{}\n", encoding="utf-8")
        dates = logger.list_dates()
        assert dates == sorted(dates, reverse=True)

    def test_read_returns_empty_for_nonexistent_file(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        future = date.today() + timedelta(days=365)
        assert logger.read(d=future) == []

    def test_malformed_jsonl_lines_are_skipped(self, tmp_path):
        m = _get_qa_logger_module()
        logger = m.QALogger(log_dir=tmp_path)
        today = date.today().isoformat()
        path = tmp_path / f"qa_{today}.jsonl"
        path.write_text('{"good": 1}\nnot json\n{"also_good": 2}\n', encoding="utf-8")
        records = logger.read()
        assert len(records) == 2
