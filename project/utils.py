import os
import shutil
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
# converter once per process and reuse it across every PDF.
_converter = None

def _get_converter():
    global _converter
    if _converter is None:
        # Lazy import so merely importing utils (e.g. in unit tests) does not require
        # docling / torch to be installed.
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()
    return _converter

def pdf_to_markdown(pdf_path, output_dir):
    # Docling reconstructs table cell structure (TableFormer) and reading order far
    # better than a flat text dump, so merged-cell / multi-column / page-spanning
    # tables survive into the markdown the chunker consumes.
    result = _get_converter().convert(str(pdf_path))
    md = result.document.export_to_markdown()
    md_cleaned = md.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='ignore')
    # Build the output name as stem + ".md" (string), NOT Path.with_suffix(): real
    # notice filenames often contain dots ("1. 공고", "매뉴얼24.5.23.") and with_suffix
    # would treat the text after the first dot as an extension and truncate it.
    output_path = Path(output_dir) / (Path(str(pdf_path)).stem + ".md")
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
