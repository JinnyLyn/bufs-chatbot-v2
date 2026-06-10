"""Unit tests for project/utils.py — estimate_context_tokens and clear_directory_contents.

Note: utils.py also imports pymupdf/pymupdf4llm for pdf_to_markdown, but those
functions are NOT exercised here (they require real PDF files and are out of
scope for offline unit tests). We test only the two offline-safe functions.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _import_utils():
    import utils
    return utils


class TestEstimateContextTokens:
    def _make_msg(self, content: str):
        """Minimal stand-in for a langchain BaseMessage."""
        msg = MagicMock()
        msg.content = content
        return msg

    def test_returns_integer(self):
        utils = _import_utils()
        msgs = [self._make_msg("Hello world")]
        result = utils.estimate_context_tokens(msgs)
        assert isinstance(result, int)

    def test_empty_list_returns_zero(self):
        utils = _import_utils()
        assert utils.estimate_context_tokens([]) == 0

    def test_single_short_message_token_count_positive(self):
        utils = _import_utils()
        msgs = [self._make_msg("졸업학점은 몇 학점인가요?")]
        assert utils.estimate_context_tokens(msgs) > 0

    def test_longer_message_has_more_tokens_than_shorter(self):
        utils = _import_utils()
        short = [self._make_msg("Hi")]
        long = [self._make_msg("Hi " * 100)]
        assert utils.estimate_context_tokens(long) > utils.estimate_context_tokens(short)

    def test_messages_without_content_attr_are_skipped(self):
        """Messages lacking a content attribute should not cause a crash."""
        utils = _import_utils()
        msg = MagicMock(spec=[])  # no content attribute
        result = utils.estimate_context_tokens([msg])
        assert result == 0

    def test_messages_with_none_content_are_skipped(self):
        utils = _import_utils()
        msg = MagicMock()
        msg.content = None
        result = utils.estimate_context_tokens([msg])
        assert result == 0

    def test_multiple_messages_tokens_sum_correctly(self):
        utils = _import_utils()
        msgs = [self._make_msg("Hello"), self._make_msg("World")]
        total = utils.estimate_context_tokens(msgs)
        individual = sum(
            utils.estimate_context_tokens([m]) for m in msgs
        )
        assert total == individual


class TestClearDirectoryContents:
    def test_deletes_files_inside_directory(self, tmp_path):
        utils = _import_utils()
        (tmp_path / "file.txt").write_text("data")
        utils.clear_directory_contents(tmp_path)
        assert not any(tmp_path.iterdir())

    def test_deletes_subdirectories_recursively(self, tmp_path):
        utils = _import_utils()
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested")
        utils.clear_directory_contents(tmp_path)
        assert not subdir.exists()

    def test_directory_itself_is_preserved(self, tmp_path):
        utils = _import_utils()
        (tmp_path / "x.txt").write_text("x")
        utils.clear_directory_contents(tmp_path)
        assert tmp_path.is_dir()

    def test_no_error_on_empty_directory(self, tmp_path):
        utils = _import_utils()
        utils.clear_directory_contents(tmp_path)  # should not raise

    def test_no_error_when_directory_does_not_exist(self, tmp_path):
        utils = _import_utils()
        nonexistent = tmp_path / "ghost"
        utils.clear_directory_contents(nonexistent)  # should not raise

    def test_clears_multiple_files_and_subdirs(self, tmp_path):
        utils = _import_utils()
        for i in range(3):
            (tmp_path / f"file_{i}.txt").write_text(f"content {i}")
        (tmp_path / "sub1" / "sub2").mkdir(parents=True)
        (tmp_path / "sub1" / "sub2" / "deep.txt").write_text("deep")
        utils.clear_directory_contents(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_accepts_string_path(self, tmp_path):
        """clear_directory_contents must accept str as well as Path."""
        utils = _import_utils()
        (tmp_path / "f.txt").write_text("data")
        utils.clear_directory_contents(str(tmp_path))
        assert not any(tmp_path.iterdir())
