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
        if not content or content in _EMPTY_MARKERS or content.startswith(("RETRIEVAL_ERROR", "PARENT_RETRIEVAL_ERROR")):
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
