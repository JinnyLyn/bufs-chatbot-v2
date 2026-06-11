"""Tripwire: grep-target literals cited in the runbooks must exist in source.

docs/debugging/*.md and debug/README.md tell operators to grep for specific
log messages / flag names during incidents. The docs once cited a string that
never existed in code ("[QA-write-failure]"); this pins today's citations so
rewording a log message fails here instead of silently breaking a runbook.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# source file → literals the docs tell operators to grep for / expect
_PINNED: dict[str, tuple[str, ...]] = {
    # qa_logger.md: real write-failure text (ERROR) vs wrapper text (WARNING)
    "project/api/qa_logger.py": ("Q&A log write failed",),
    "project/api/chat.py": ("Q&A log failed", "[chat-IN]", "[chat-OUT]", "[chat-ERR]"),
    # session/README verdict-flag tables
    "debug/session.py": ("REFUSE", "NO-RESULTS", "SENTINEL", "RUNAWAY", "ORPHAN"),
}


@pytest.mark.parametrize(("rel_path", "literals"), sorted(_PINNED.items()))
def test_doc_cited_literals_exist_in_source(
    rel_path: str, literals: tuple[str, ...]
) -> None:
    text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    missing = [lit for lit in literals if lit not in text]
    assert not missing, (
        f"{rel_path} no longer contains {missing} — update the runbooks that "
        "cite these strings (docs/debugging/, debug/README.md) in the same PR"
    )
