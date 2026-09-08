"""DocumentManager.add_documents — source/target path containment (CodeQL py/path-injection).

The target file name is derived from a caller-supplied source path. Every write must land as
a direct child of ``config.MARKDOWN_DIR`` and, when ``source_root`` is given, every read must
come from inside that root. ``utils.confined_path`` itself is unit-tested in
tests/test_utils.py; these tests cover how add_documents applies it.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("tiktoken", reason="utils imports tiktoken")


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    """A DocumentManager over a fresh KB dir with a fake RAG system: ``.manager``, ``.rag``, ``.dir``."""
    import config
    import core.document_manager as dm_mod

    kb_dir = tmp_path / "kb"
    monkeypatch.setattr(config, "MARKDOWN_DIR", str(kb_dir))
    monkeypatch.setattr(config, "KB_EXCLUDE_SOURCES", frozenset())
    monkeypatch.setattr(dm_mod, "_invalidate_parent_scope_cache", lambda: None)  # cache hygiene, offline

    rag = MagicMock()
    rag.collection_name = "test"
    rag.chunker.create_chunks_single.return_value = (["parent"], ["child"])
    return SimpleNamespace(manager=dm_mod.DocumentManager(rag), rag=rag, dir=kb_dir)


def test_add_md_copies_into_kb_dir_and_indexes(kb, tmp_path):
    src = tmp_path / "notice.md"
    src.write_text("# hello")

    assert kb.manager.add_documents([str(src)]) == (1, 0)
    assert (kb.dir / "notice.md").read_text() == "# hello"
    kb.rag.chunker.create_chunks_single.assert_called_once()
    kb.rag.vector_db.get_collection.return_value.add_documents.assert_called_once_with(["child"])
    kb.rag.parent_store.save_many.assert_called_once_with(["parent"])


def test_add_md_with_odd_name_still_lands_inside_kb_dir(kb, tmp_path):
    """Dots and backslashes in a name are one path component on POSIX; the target must still
    be a direct child of the KB dir, never anything above it."""
    src = tmp_path / "..\\..\\evil.md"
    src.write_text("x")
    before = {p.name for p in tmp_path.iterdir()}

    added, _skipped = kb.manager.add_documents([str(src)])

    assert added == 1
    written = list(kb.dir.iterdir())
    assert len(written) == 1 and written[0].parent == kb.dir
    assert {p.name for p in tmp_path.iterdir()} == before  # nothing new beside kb/ itself


def test_add_md_refuses_to_write_through_dangling_symlink_out_of_kb(kb, tmp_path, caplog):
    """A dangling ``kb/notice.md`` → outside passes ``exists()`` (False) and a plain copy would
    create the outside file. The containment check rejects the resolved path and logs it."""
    outside = tmp_path / "escaped.md"
    (kb.dir / "notice.md").symlink_to(outside)  # dangling: target does not exist yet
    src = tmp_path / "notice.md"
    src.write_text("payload")

    with caplog.at_level(logging.WARNING, logger="core.document_manager"):
        assert kb.manager.add_documents([str(src)]) == (0, 1)
    assert not outside.exists()
    assert "markdown target escapes" in caplog.text
    kb.rag.chunker.create_chunks_single.assert_not_called()


def test_add_skips_existing_target(kb, tmp_path):
    (kb.dir / "notice.md").write_text("old")
    src = tmp_path / "notice.md"
    src.write_text("new")

    assert kb.manager.add_documents([str(src)]) == (0, 1)
    assert (kb.dir / "notice.md").read_text() == "old"
    kb.rag.chunker.create_chunks_single.assert_not_called()


# ---------------------------------------------------------------------------
# source_root — the read side (Gradio passes its upload cache)
# ---------------------------------------------------------------------------

def test_source_root_accepts_paths_inside_it(kb, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "notice.md"
    src.write_text("# up")

    assert kb.manager.add_documents([str(src)], source_root=str(uploads)) == (1, 0)
    assert (kb.dir / "notice.md").read_text() == "# up"


def test_source_root_rejects_path_outside_it_and_logs(kb, tmp_path, caplog):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    secret = tmp_path / "secret.md"  # e.g. a server file a crafted upload value points at
    secret.write_text("keep out")

    with caplog.at_level(logging.WARNING, logger="core.document_manager"):
        assert kb.manager.add_documents([str(secret)], source_root=str(uploads)) == (0, 1)
    assert list(kb.dir.iterdir()) == []
    assert "outside source root" in caplog.text and str(secret) in caplog.text


def test_source_root_rejects_symlink_leading_out_of_it(kb, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("keep out")
    (uploads / "link.md").symlink_to(secret)

    assert kb.manager.add_documents([str(uploads / "link.md")], source_root=str(uploads)) == (0, 1)
    assert list(kb.dir.iterdir()) == []


def test_add_pdf_converts_resolved_source_into_kb_dir(kb, tmp_path, monkeypatch):
    """PDFs go straight to pdf_to_markdown (no glob expansion) with the checked source path."""
    import core.document_manager as dm_mod

    calls = []

    def fake_pdf_to_markdown(pdf_path, output_dir, out_name=None):
        calls.append((pdf_path, output_dir, out_name))
        (output_dir / out_name).write_text("# pdf")

    monkeypatch.setattr(dm_mod, "pdf_to_markdown", fake_pdf_to_markdown)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")

    assert kb.manager.add_documents([str(src)], source_root=str(uploads)) == (1, 0)
    assert calls == [(os.path.realpath(src), kb.dir, "doc.md")]
    assert (kb.dir / "doc.md").read_text() == "# pdf"


def test_add_pdf_via_symlink_inside_source_root_keeps_the_link_name(kb, tmp_path, monkeypatch):
    """A symlink inside the upload root is read through its target, but the KB file is named
    after the link (what the caller asked for), so md_path and the written file agree and
    the chunker finds it — no orphan ``<target-stem>.md``."""
    import core.document_manager as dm_mod

    def fake_pdf_to_markdown(pdf_path, output_dir, out_name=None):
        (output_dir / out_name).write_text("# pdf")

    monkeypatch.setattr(dm_mod, "pdf_to_markdown", fake_pdf_to_markdown)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "real-name.pdf").write_bytes(b"%PDF-1.4")
    (uploads / "a.pdf").symlink_to(uploads / "real-name.pdf")

    assert kb.manager.add_documents([str(uploads / "a.pdf")], source_root=str(uploads)) == (1, 0)
    assert sorted(p.name for p in kb.dir.iterdir()) == ["a.md"]
    kb.rag.chunker.create_chunks_single.assert_called_once_with(kb.dir / "a.md")
