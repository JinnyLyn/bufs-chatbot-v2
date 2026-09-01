from pathlib import Path
import shutil
import config
from utils import pdfs_to_markdowns, clear_directory_contents

class DocumentManager:

    def __init__(self, rag_system):
        self.rag_system = rag_system
        self.markdown_dir = Path(config.MARKDOWN_DIR)
        self.markdown_dir.mkdir(parents=True, exist_ok=True)
        
    def add_documents(self, document_paths, progress_callback=None):
        if not document_paths:
            return 0, 0
            
        document_paths = [document_paths] if isinstance(document_paths, str) else document_paths
        document_paths = [p for p in document_paths if p and Path(p).suffix.lower() in [".pdf", ".md"]]
        
        if not document_paths:
            return 0, 0
            
        added = 0
        skipped = 0
            
        for i, doc_path in enumerate(document_paths):
            if progress_callback:
                progress_callback((i + 1) / len(document_paths), f"Processing {Path(doc_path).name}")
                
            doc_name = Path(doc_path).stem

            # KB scope guard (#108): skip out-of-scope sources BEFORE materializing markdown.
            # If we let it copy/convert first and only drop it at the chunker, the orphaned .md
            # stays in markdown_docs — inflating get_markdown_files()/health counts with a doc
            # that was never indexed, and blocking a later re-add (md_path.exists() short-circuit)
            # if it's removed from the exclusion set.
            if doc_name in config.KB_EXCLUDE_SOURCES:
                skipped += 1
                continue

            md_path = self.markdown_dir / f"{doc_name}.md"

            if md_path.exists():
                skipped += 1
                continue
                
            try:            
                if Path(doc_path).suffix.lower() == ".md":
                    shutil.copy(doc_path, md_path)
                else:
                    pdfs_to_markdowns(str(doc_path), overwrite=False)            
                parent_chunks, child_chunks = self.rag_system.chunker.create_chunks_single(md_path)
                
                if not child_chunks:
                    skipped += 1
                    continue
                
                collection = self.rag_system.vector_db.get_collection(self.rag_system.collection_name)
                collection.add_documents(child_chunks)
                self.rag_system.parent_store.save_many(parent_chunks)
                
                added += 1

            except Exception as e:
                print(f"Error processing {doc_path}: {e}")
                skipped += 1

        _invalidate_parent_scope_cache()
        return added, skipped
    
    def get_markdown_files(self):
        if not self.markdown_dir.exists():
            return []
        return sorted([p.name.replace(".md", ".pdf") for p in self.markdown_dir.glob("*.md")])
    
    def clear_all(self):
        self.markdown_dir.mkdir(parents=True, exist_ok=True)
        clear_directory_contents(self.markdown_dir)
        
        self.rag_system.parent_store.clear_store()
        self.rag_system.vector_db.delete_collection(self.rag_system.collection_name)
        self.rag_system.vector_db.create_collection(self.rag_system.collection_name)
        _invalidate_parent_scope_cache()


def _invalidate_parent_scope_cache():
    """The OCU parent verdicts in rag_agent.tools are cached for the life of the
    process (safe for doc_sync, which restarts the server) — this in-process mutation
    path must drop them or verdicts go stale against the rewritten/wiped store."""
    try:
        from rag_agent.tools import _parent_ocu_flag  # lazy: avoids an import cycle
        _parent_ocu_flag.cache_clear()
    except Exception:  # noqa: BLE001 — cache hygiene must never fail a KB mutation
        pass
