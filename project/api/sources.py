"""Turn raw agent tool output into the `results` / `source_urls` shapes the
CamChat SourcePanel renders.

The agent's retrieval tools (`search_child_chunks`, `retrieve_parent_chunks`)
return plain text blocks shaped like:

    Parent ID: <id>
    File Name: <name>.pdf
    Content: <text...>

We parse those blocks back into structured citation items. agentic-RAG has no
notion of external notice URLs, so `source_urls` stays empty and everything
useful goes into `results`.
"""

import re

_FILE_RE = re.compile(r"File Name:\s*(.+)")
_EMPTY_MARKERS = {
    "NO_RELEVANT_CHUNKS",
    "NO_PARENT_DOCUMENT",
    "NO_PARENT_DOCUMENTS",
}


def _new_result(source: str, text: str) -> dict:
    return {
        "text": text[:600],
        "score": 0.0,
        "source": source,
        "page_number": 0,
        "doc_type": "document",
        "in_context": True,
        "section_path": "",
        "source_url": "",
        "title": source,
        "post_date": "",
        "faq_id": "",
        "faq_question": "",
        "faq_answer": "",
    }


def parse_tool_results(tool_contents: list[str], max_items: int = 10):
    """Return (results, source_urls) parsed from raw tool result strings."""
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for content in tool_contents:
        content = (content or "").strip()
        if not content or content in _EMPTY_MARKERS or content.startswith(
                ("RETRIEVAL_ERROR", "PARENT_RETRIEVAL_ERROR", "SEARCH_BUDGET_EXCEEDED")):
            continue

        # Each retrieved chunk starts with "Parent ID:". Split on that boundary.
        blocks = re.split(r"\n\n(?=Parent ID:)", content)
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            fname_match = _FILE_RE.search(block)
            source = fname_match.group(1).strip() if fname_match else ""

            ci = block.find("Content:")
            text = block[ci + len("Content:"):].strip() if ci != -1 else block

            key = (source, text[:80])
            if key in seen:
                continue
            seen.add(key)
            results.append(_new_result(source, text))

    # agentic-RAG sources are local filenames, not browseable URLs.
    source_urls: list[dict] = []
    return results[:max_items], source_urls


# --- answer post-processing -------------------------------------------------
# The prompts forbid the old sources footer ("---\n**출처:**\n- file.pdf …"), but a
# disobedient generation can still emit one — the compression context keeps feeding
# "### filename.pdf" headers to the model. The SourcePanel shows sources from
# structured metadata, so a leaked footer only duplicates it; strip it from the
# shipped answer so the guarantee is structural, not best-effort.
#
# Deliberately a bottom-up line scan with small anchored per-line patterns, NOT one
# multi-line regex over the whole answer: the answer is model output shaped by user
# input, and a single regex with alternating repeated groups was flagged by CodeQL
# (py/redos) as exponential-backtracking territory. The scan is linear by
# construction. A block counts as a footer ONLY when list/filename lines sit
# directly under a 출처/Sources heading line at the very end of the answer.
_FOOTER_HEADING_RE = re.compile(r"^[ \t]*\*{0,2}(?:출처|Sources?)\*{0,2}[ \t]*:?\*{0,2}[ \t]*$")
_FOOTER_ITEM_RE = re.compile(r"^[ \t]*(?:[-*•]|\d+\.)[ \t]+\S")
_FOOTER_FILENAME_RE = re.compile(r"\.(?:pdf|docx?|txt|md)[ \t]*$", re.IGNORECASE)
_FOOTER_RULE_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$")


def strip_source_footer(answer: str) -> str:
    """Remove a trailing 출처/Sources footer block; leave everything else untouched."""
    lines = answer.split("\n")
    i = len(lines) - 1
    saw_item = False
    # Walk up over the candidate item block (list items, bare filename lines, blanks).
    while i >= 0:
        line = lines[i]
        if not line.strip():
            i -= 1
            continue
        if _FOOTER_ITEM_RE.match(line) or (_FOOTER_FILENAME_RE.search(line) and line.strip()):
            saw_item = True
            i -= 1
            continue
        break
    if not saw_item or i < 0 or not _FOOTER_HEADING_RE.match(lines[i]):
        return answer
    i -= 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i >= 0 and _FOOTER_RULE_RE.match(lines[i]):
        i -= 1
    stripped = "\n".join(lines[: i + 1]).rstrip()
    return stripped or answer  # never blank the answer if it was footer-only
