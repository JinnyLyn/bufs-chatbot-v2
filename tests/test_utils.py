"""Unit tests for project/utils.py — estimate_context_tokens, clear_directory_contents,
confined_path, and the guarded write at the end of pdf_to_markdown.

Note: pdf_to_markdown lazily imports docling inside _get_converter(); the conversion itself
is NOT exercised here (real PDFs + heavy docling/torch deps). The containment tests mock the
converter and only check where the markdown lands.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestGetConverter:
    """The Docling converter is a lazily-built, process-wide singleton. These tests
    mock the docling import so they run offline (no docling/torch needed)."""

    def test_singleton_returns_same_instance_and_builds_once(self):
        utils = _import_utils()
        mock_cls = MagicMock()
        fake_module = MagicMock(DocumentConverter=mock_cls)
        saved = utils._converter
        try:
            utils._converter = None  # reset before exercising the lazy init
            with patch.dict("sys.modules", {"docling.document_converter": fake_module}):
                c1 = utils._get_converter()
                c2 = utils._get_converter()
            assert c1 is c2  # same cached instance
            mock_cls.assert_called_once()  # constructor invoked exactly once
        finally:
            utils._converter = saved  # don't leak the mock into other tests


class TestConfinedPath:
    """confined_path is the one containment primitive (KB target, upload source, parent store)."""

    def test_accepts_relative_child(self, tmp_path):
        utils = _import_utils()
        root = tmp_path / "kb"
        root.mkdir()
        assert utils.confined_path(root, "notice.md") == os.path.join(os.path.realpath(root), "notice.md")

    def test_accepts_absolute_child_after_normalization(self, tmp_path):
        utils = _import_utils()
        root = tmp_path / "kb"
        (root / "sub").mkdir(parents=True)
        messy = str(root / "sub" / ".." / "b.md")
        assert utils.confined_path(root, messy) == os.path.join(os.path.realpath(root), "b.md")

    @pytest.mark.parametrize("escape", ["../x.md", "/etc/passwd", "..", "."])
    def test_rejects_escapes_and_root_itself(self, tmp_path, escape):
        utils = _import_utils()
        root = tmp_path / "kb"
        root.mkdir()
        assert utils.confined_path(root, escape) is None

    def test_rejects_symlink_pointing_outside(self, tmp_path):
        utils = _import_utils()
        root = tmp_path / "kb"
        root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        (root / "link.md").symlink_to(outside)
        assert utils.confined_path(root, "link.md") is None

    def test_rejects_sibling_whose_name_has_root_as_prefix(self, tmp_path):
        """``/kb-evil/x`` starts with ``/kb`` as a string; the separator-terminated check
        must not be fooled by that."""
        utils = _import_utils()
        root = tmp_path / "kb"
        root.mkdir()
        sibling = tmp_path / "kb-evil"
        sibling.mkdir()
        assert utils.confined_path(root, str(sibling / "x.md")) is None

    def test_root_slash_rejects_everything(self):
        """No containment exists under ``/``; fail closed instead of accepting every path."""
        utils = _import_utils()
        assert utils.confined_path("/", "/etc/passwd") is None
        assert utils.confined_path("/", "etc/passwd") is None

    def test_accepts_bytes_and_pathlike_inputs(self, tmp_path):
        utils = _import_utils()
        root = tmp_path / "kb"
        root.mkdir()
        expected = os.path.join(os.path.realpath(root), "a.md")
        assert utils.confined_path(root, b"a.md") == expected
        assert utils.confined_path(str(root), Path("a.md")) == expected


class TestPdfToMarkdownContainment:
    """The write at the end of pdf_to_markdown is where PDF text lands on disk; it must refuse
    a target that resolves outside output_dir. Conversion is mocked."""

    def _mock_conversion(self, monkeypatch, utils, text="# converted"):
        result = MagicMock()
        result.document.export_to_markdown.return_value = text
        converter = MagicMock()
        converter.convert.return_value = result
        monkeypatch.setattr(utils, "_get_converter", lambda: converter)
        monkeypatch.setattr(utils, "_supplement_dropped_pages", lambda md, *_args: md)

    def test_writes_stem_plus_md_inside_output_dir(self, tmp_path, monkeypatch):
        utils = _import_utils()
        self._mock_conversion(monkeypatch, utils)
        out = tmp_path / "kb"
        out.mkdir()
        utils.pdf_to_markdown(tmp_path / "1. 공고.pdf", out)
        assert (out / "1. 공고.md").read_text(encoding="utf-8") == "# converted"

    def test_out_name_overrides_the_derived_name(self, tmp_path, monkeypatch):
        utils = _import_utils()
        self._mock_conversion(monkeypatch, utils)
        out = tmp_path / "kb"
        out.mkdir()
        utils.pdf_to_markdown(tmp_path / "real-name.pdf", out, out_name="a.md")
        assert sorted(p.name for p in out.iterdir()) == ["a.md"]

    def test_refuses_dangling_symlink_target_out_of_dir(self, tmp_path, monkeypatch):
        utils = _import_utils()
        self._mock_conversion(monkeypatch, utils)
        out = tmp_path / "kb"
        out.mkdir()
        outside = tmp_path / "escaped.md"
        (out / "notice.md").symlink_to(outside)  # dangling: a plain write would create outside
        with pytest.raises(ValueError):
            utils.pdf_to_markdown(tmp_path / "notice.pdf", out)
        assert not outside.exists()
