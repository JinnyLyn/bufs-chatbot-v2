"""Render qa_completed.json into a readable Markdown table (the deliverable)."""
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(HERE, "qa_completed.json"), encoding="utf-8"))


def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


hdr = ["번호", "Question", "Gold Intent", "Gold Document", "Gold Chunk ID (KB)",
       "Expected Answer", "Must Include", "Must Not Include", "Difficulty",
       "Category", "Retrieval", "Generation"]
lines = ["| " + " | ".join(hdr) + " |",
         "|" + "|".join(["---"] * len(hdr)) + "|"]
for r in rows:
    lines.append("| " + " | ".join(esc(x) for x in [
        r["id"], r["question"], r["gold_intent"], r["gold_document"],
        r["gold_chunk_id"], r["expected_answer"], r["must_include"],
        r["must_not_include"] or "—", r["difficulty"], r["category"],
        r["retrieval_success"], r["generation_success"],
    ]) + " |")

open(os.path.join(HERE, "qa_completed.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("wrote qa/qa_completed.md", len(rows), "rows")
