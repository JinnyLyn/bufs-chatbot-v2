"""DocumentManager — path containment of the KB markdown target (CodeQL py/path-injection).

``add_documents`` derives the target file name from a caller-supplied source path. Every
write must land as a direct child of ``config.MARKDOWN_DIR``; nothing outside it may be
created or overwritten, not even through a symlink that sits inside the directory.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

pytest.importorskip("tiktoken", reason="utils imports tiktoken")

from core.document_manager import DocumentManager, confined_path  # noqa: E402


# ---------------------------------------------------------------------------
# confined_path
# ---------------------------------------------------------------------------

def test_confined_path_accepts_relative_child(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    assert confined_path(root, "notice.md") == os.path.join(os.path.realpath(root), "notice.md")


def test_confined_path_accepts_absolute_child_after_normalization(tmp_path):
    root = tmp_path / "kb"
    (root / "sub").mkdir(parents=True)
    messy = str(root / "sub" / ".." / "b.md")
    assert confined_path(root, messy) == os.path.join(os.path.realpath(root), "b.md")


@pytest.mark.parametrize("escape", ["../x.md", "/etc/passwd", "..", "."])
def test_confined_path_rejects_escapes_and_root_itself(tmp_path, escape):
    root = tmp_path / "kb"
    root.mkdir()
    assert confined_path(root, escape) is None


def test_confined_path_rejects_symlink_pointing_outside(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (root / "link.md").symlink_to(outside)
    assert confined_path(root, "link.md") is None


def test_confined_path_rejects_sibling_with_root_as_prefix(tmp_path):
    """``/kb-evil/x`` starts with ``/kb`` as a string; the separator-terminated check must
    not be fooled by that."""
    root = tmp_path / "kb"
    root.mkdir()
    sibling = tmp_path / "kb-evil"
    sibling.mkdir()
    assert confined_path(root, str(sibling / "x.md")) is None


# ---------------------------------------------------------------------------
# add_documents
# ---------------------------------------------------------------------------

def _manager(tmp_path, monkeypatch):
    import config
    import core.document_manager as dm_mod

    kb = tmp_path / "kb"
    monkeypatch.setattr(config, "MARKDOWN_DIR", str(kb))
    monkeypatch.setattr(config, "KB_EXCLUDE_SOURCES", frozenset())
    # The lazy rag_agent import is cache hygiene, not under test; keep the test offline.
    monkeypatch.setattr(dm_mod, "_invalidate_parent_scope_cache", lambda: None)

    rag = MagicMock()
    rag.collection_name = "test"
    rag.chunker.create_chunks_single.return_value = (["parent"], ["child"])
    return DocumentManager(rag), rag, kb


def test_add_md_copies_into_kb_dir_and_indexes(tmp_path, monkeypatch):
    manager, rag, kb = _manager(tmp_path, monkeypatch)
    src = tmp_path / "notice.md"
    src.write_text("# hello")

    assert manager.add_documents([str(src)]) == (1, 0)
    assert (kb / "notice.md").read_text() == "# hello"
    rag.chunker.create_chunks_single.assert_called_once()
    rag.vector_db.get_collection.return_value.add_documents.assert_called_once_with(["child"])
    rag.parent_store.save_many.assert_called_once_with(["parent"])


def test_add_md_with_odd_name_still_lands_inside_kb_dir(tmp_path, monkeypatch):
    """A source name full of dots and backslashes is a single path component on POSIX; the
    target must still be a direct child of the KB dir, never anything above it."""
    manager, _, kb = _manager(tmp_path, monkeypatch)
    src = tmp_path / "..\\..\\evil.md"
    src.write_text("x")
    before = {p.name for p in tmp_path.iterdir()}

    added, _skipped = manager.add_documents([str(src)])

    assert added == 1
    written = list(kb.iterdir())
    assert len(written) == 1 and written[0].parent == kb
    assert {p.name for p in tmp_path.iterdir()} == before  # nothing new beside kb/ itself


def test_add_md_refuses_to_write_through_dangling_symlink_out_of_kb(tmp_path, monkeypatch):
    """A dangling ``kb/notice.md`` → outside would pass ``exists()`` (False) and the copy would
    create the outside file. The containment check rejects the resolved path instead."""
    manager, rag, kb = _manager(tmp_path, monkeypatch)
    outside = tmp_path / "escaped.md"
    (kb / "notice.md").symlink_to(outside)  # dangling: target does not exist yet
    src = tmp_path / "notice.md"
    src.write_text("payload")

    assert manager.add_documents([str(src)]) == (0, 1)
    assert not outside.exists()
    rag.chunker.create_chunks_single.assert_not_called()


def test_add_skips_existing_target(tmp_path, monkeypatch):
    manager, rag, kb = _manager(tmp_path, monkeypatch)
    (kb / "notice.md").write_text("old")
    src = tmp_path / "notice.md"
    src.write_text("new")

    assert manager.add_documents([str(src)]) == (0, 1)
    assert (kb / "notice.md").read_text() == "old"
    rag.chunker.create_chunks_single.assert_not_called()
