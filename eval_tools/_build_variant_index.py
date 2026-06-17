"""Build a sparse-tokenizer A/B *variant* collection alongside the baseline.

Unlike reindex.py this does NOT wipe the Qdrant DB or the parent store — it adds a
new named collection into the existing embedded DB so several tokenizer variants can
coexist for side-by-side eval. Dense (bge-m3) vectors and child-chunk text are
identical across variants (deterministic chunker on the same markdown_docs), so only
the sparse leg differs. The shared parent store is reused (parent ids are deterministic
'<doc>_parent_<n>'), so retrieve_parent_chunks works for every variant.

Config comes from env (read by config.py):
    SPARSE_MODEL   kiwi | okt | whitespace | Qdrant/bm25 | Qdrant/bm42-all-minilm-l6-v2-attentions
    SPARSE_IDF     true/false  (Qdrant sparse modifier="idf")
    CHILD_COLLECTION  the variant collection name to create

Usage (run from MAIN repo or the worktree; stop any backend on this DB first):
    SPARSE_MODEL=kiwi SPARSE_IDF=true CHILD_COLLECTION=document_child_chunks__kiwi_idf \
        python eval_tools/_build_variant_index.py
Add --force to drop and rebuild an existing variant collection.
"""
import hashlib
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "project")
sys.path.insert(0, _PROJECT)

from dotenv import load_dotenv

load_dotenv(os.path.join(_PROJECT, ".env"))

from pathlib import Path

import config
from db.parent_store_manager import ParentStoreManager
from db.vector_db_manager import VectorDbManager
from document_chunker import DocumentChuncker


def main() -> None:
    force = "--force" in sys.argv
    coll = config.CHILD_COLLECTION
    print(f"variant: SPARSE_MODEL={config.SPARSE_MODEL!r}  SPARSE_IDF={config.SPARSE_IDF}  collection={coll!r}")

    md_files = sorted(Path(config.MARKDOWN_DIR).glob("*.md"))
    print(f"markdown_docs: {len(md_files)} .md file(s)")
    if not md_files:
        print("No .md files — aborting.")
        return

    print("Loading embedding model + opening Qdrant...")
    vdb = VectorDbManager()

    # Resumable / idempotent: skip if the variant collection already has points.
    client = vdb._VectorDbManager__client  # name-mangled private client
    if client.collection_exists(coll):
        n = client.get_collection(coll).points_count
        if n and not force:
            print(f"Collection {coll!r} already has {n} points — skipping (use --force to rebuild).")
            return
        if force:
            print(f"--force: dropping existing {coll!r} ({n} points)")
            vdb.delete_collection(coll)

    vdb.create_collection(coll)
    collection = vdb.get_collection(coll)

    # Reuse the shared parent store (already populated by the baseline build). Re-save
    # is harmless/idempotent but we leave it untouched to avoid churning tracked files.
    ParentStoreManager()

    chunker = DocumentChuncker()
    seen: dict[str, str] = {}
    docs = children_total = 0
    for md in md_files:
        digest = hashlib.md5(md.read_bytes()).hexdigest()
        if digest in seen:
            print(f"  (skip exact-dup of '{seen[digest]}') {md.name}")
            continue
        seen[digest] = md.name
        _parents, children = chunker.create_chunks_single(md)
        if not children:
            print(f"  (no chunks) {md.name}")
            continue
        collection.add_documents(children)
        docs += 1
        children_total += len(children)
        print(f"  ok  {md.name}: {len(children)} children")

    final = client.get_collection(coll).points_count
    print(f"\nDone. {docs} docs -> {children_total} child chunks. Collection {coll!r} now holds {final} points.")


if __name__ == "__main__":
    main()
