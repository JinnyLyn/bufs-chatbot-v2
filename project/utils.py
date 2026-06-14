import os
import shutil
import threading
import config
from pathlib import Path
import glob
import tiktoken


def clear_directory_contents(directory: Path) -> None:
    """Delete everything under directory but not the directory itself (safe for Docker volume / bind mount roots)."""
    directory = Path(directory)
    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Docling loads layout + TableFormer models on first use (expensive). Build the
# converter once per process and reuse it across every PDF. The lock makes the
# lazy init safe if pdf_to_markdown is ever called from concurrent threads.
_converter = None
_converter_lock = threading.Lock()


def _get_converter():
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                # Lazy import so merely importing utils (e.g. in unit tests) does not
                # require docling / torch to be installed.
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


def _docling_covered_pages(dl_doc) -> set:
    """Page numbers (1-based) Docling actually produced content for, read from item
    provenance. Pages it std::bad_alloc'd on render have NO items → absent from this set."""
    covered = set()
    for attr in ("texts", "tables", "pictures", "groups"):
        for item in getattr(dl_doc, attr, None) or []:
            for prov in getattr(item, "prov", None) or []:
                pno = getattr(prov, "page_no", None)
                if pno:
                    covered.add(pno)
    return covered


def _supplement_dropped_pages(md: str, pdf_path, dl_doc) -> str:
    """Docling can std::bad_alloc while rendering large/complex pages and SILENTLY DROP them
    (observed on 학사안내 pp.86-96, which include the 학부 전화번호 directory — neither do_ocr=
    False nor a lower images_scale avoids it). Use Docling's own page provenance to find pages
    it produced NOTHING for, and append those pages' text from pymupdf's text layer — recovering
    lost content without duplicating pages Docling converted fine."""
    try:
        import pymupdf
    except Exception:
        return md
    covered = _docling_covered_pages(dl_doc)
    extra = []
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            for page in doc:
                if (page.number + 1) in covered:
                    continue
                txt = page.get_text().strip()
                if len(txt) >= 40:
                    extra.append(f"\n\n## 원문 보충 (p.{page.number + 1})\n\n{txt}")
    except Exception:
        return md
    return md + "".join(extra)


def pdf_to_markdown(pdf_path, output_dir):
    # Docling reconstructs table cell structure (TableFormer) and reading order far
    # better than a flat text dump, so merged-cell / multi-column / page-spanning
    # tables survive into the markdown the chunker consumes.
    result = _get_converter().convert(str(pdf_path))
    md = result.document.export_to_markdown()
    md = _supplement_dropped_pages(md, pdf_path, result.document)   # recover pages Docling dropped
    md_cleaned = md.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='ignore')
    # Build the output name as stem + ".md" (string), NOT Path.with_suffix(): real
    # notice filenames often contain dots ("1. 공고", "매뉴얼24.5.23.") and with_suffix
    # would treat the text after the first dot as an extension and truncate it.
    output_path = Path(output_dir) / (Path(pdf_path).stem + ".md")
    output_path.write_bytes(md_cleaned.encode('utf-8'))

def pdfs_to_markdowns(path_pattern, overwrite: bool = False):
    output_dir = Path(config.MARKDOWN_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Accept an explicit file path (which may contain glob metacharacters like the
    # [ ] common in real notice filenames) or a glob pattern.
    _p = Path(path_pattern)
    pdf_paths = [_p] if _p.is_file() else [Path(x) for x in glob.glob(path_pattern)]

    for pdf_path in pdf_paths:
        md_path = output_dir / (pdf_path.stem + ".md")
        if overwrite or not md_path.exists():
            pdf_to_markdown(pdf_path, output_dir)

def estimate_context_tokens(messages: list) -> int:
    try:
        encoding = tiktoken.encoding_for_model("gpt-4")
    except:
        encoding = tiktoken.get_encoding("cl100k_base")
    return sum(len(encoding.encode(str(msg.content))) for msg in messages if hasattr(msg, 'content') and msg.content)
