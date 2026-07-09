"""Rebuild the Qdrant vector DB + parent store from the CURRENT markdown_docs/ contents.

Run this after manually adding/removing .md files in markdown_docs/ — deleting a .md
does NOT remove its already-embedded chunks, so the KB must be rebuilt to match.

Unlike ingest.py this does NOT touch PDFs or re-convert anything; it just re-chunks and
re-embeds the .md files that are present. Exact-content duplicates are skipped
automatically (handles the `+`-encoded vs space-encoded filename pairs).

Usage:
    python project/reindex.py

Stop the API server first — Qdrant is embedded (single-process lock).
"""

import hashlib
import os
import shutil
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from pathlib import Path

import config
from db.parent_store_manager import ParentStoreManager
from db.vector_db_manager import VectorDbManager
from document_chunker import DocumentChuncker


def main() -> None:
    md_files = sorted(Path(config.MARKDOWN_DIR).glob("*.md"))
    print(f"markdown_docs: {len(md_files)} .md file(s)")
    if not md_files:
        print("No .md files found — aborting (run ingest.py to populate markdown_docs first).")
        return

    # Embedded Qdrant's delete_collection() does NOT reliably purge on-disk segments, so a
    # plain reindex APPENDS to stale vectors (we observed 10k+ ghost points from long-gone
    # docs). Physically remove the DB dir first to guarantee a truly clean rebuild. This must
    # happen BEFORE VectorDbManager() opens (and locks/creates) the path.
    if os.path.isdir(config.QDRANT_DB_PATH):
        print(f"Removing existing Qdrant DB dir for a clean rebuild: {config.QDRANT_DB_PATH}")
        shutil.rmtree(config.QDRANT_DB_PATH)

    print("Loading embedding model + opening Qdrant...")
    vdb = VectorDbManager()
    vdb.delete_collection(config.CHILD_COLLECTION)
    vdb.create_collection(config.CHILD_COLLECTION)
    collection = vdb.get_collection(config.CHILD_COLLECTION)

    parent_store = ParentStoreManager()
    parent_store.clear_store()

    chunker = DocumentChuncker()
    seen: dict[str, str] = {}
    docs = parents_total = children_total = 0

    for md in md_files:
        if md.stem in config.KB_EXCLUDE_SOURCES:
            print(f"  (skip, out of scope — KB_EXCLUDE_SOURCES) {md.name}")
            continue

        digest = hashlib.md5(md.read_bytes()).hexdigest()
        if digest in seen:
            print(f"  (skip exact-dup of '{seen[digest]}') {md.name}")
            continue
        seen[digest] = md.name

        parents, children = chunker.create_chunks_single(md)
        if not children:
            print(f"  (no chunks — empty/scanned?) {md.name}")
            continue
        collection.add_documents(children)
        parent_store.save_many(parents)
        docs += 1
        parents_total += len(parents)
        children_total += len(children)
        print(f"  ok  {md.name}: {len(parents)} parents, {len(children)} children")

    print(f"\nDone. Indexed {docs} unique doc(s) → {parents_total} parents, {children_total} child chunks.")


if __name__ == "__main__":
    main()
