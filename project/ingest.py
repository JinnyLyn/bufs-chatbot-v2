"""Ingest documents into the agentic-RAG knowledge base (Qdrant + parent store).

Usage:
    python project/ingest.py <path> [<path> ...] [--clear]

<path> may be a PDF/Markdown file or a directory (scanned recursively for *.pdf,
*.md). PDFs are converted to Markdown, split into parent/child chunks, embedded
into Qdrant, and their parents saved to the local parent store.

    # ingest all BUFS academic PDFs, replacing any existing KB
    python project/ingest.py ../../bufs-chatbot/data/pdfs --clear

Note: Qdrant is embedded (local file). Stop the API server before ingesting —
only one process can hold the DB at a time.
"""

import argparse
import os
import sys
from pathlib import Path

# Windows consoles default to cp949 here; the pipeline prints ✓/emoji chars. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from core.document_manager import DocumentManager
from core.rag_system import RAGSystem

_SUPPORTED = {".pdf", ".md"}


def _collect(paths: list[str]) -> list[str]:
    files: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for ext in _SUPPORTED:
                files.extend(str(x) for x in p.rglob(f"*{ext}"))
        elif p.suffix.lower() in _SUPPORTED:
            files.append(str(p))
        else:
            print(f"  (skip, unsupported) {raw}")
    # stable, de-duplicated
    return sorted(set(files))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDF/Markdown into the agentic-RAG KB.")
    parser.add_argument("paths", nargs="+", help="PDF/MD files or directories")
    parser.add_argument("--clear", action="store_true", help="clear the existing KB before ingesting")
    args = parser.parse_args()

    print("Initializing RAG system (this loads the embedding model)...")
    rs = RAGSystem()
    rs.initialize()
    dm = DocumentManager(rs)

    if args.clear:
        print("Clearing existing knowledge base...")
        dm.clear_all()

    files = _collect(args.paths)
    if not files:
        print("No .pdf or .md files found. Nothing to do.")
        return

    print(f"Found {len(files)} document(s). Ingesting...")
    added, skipped = dm.add_documents(
        files,
        progress_callback=lambda frac, desc: print(f"  [{int(frac * 100):3d}%] {desc}"),
    )
    print(f"\nDone. Added={added}  Skipped={skipped}")
    print(f"Knowledge base now holds: {len(dm.get_markdown_files())} document(s).")


if __name__ == "__main__":
    main()
