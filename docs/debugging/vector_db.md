# Module: vector_db (Qdrant search)

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

The vector database layer runs a hybrid search: dense embeddings (bge-m3) + sparse BM25,
fused via RRF. Chunks are retrieved from Qdrant, filtered by `score_threshold=0.3`, and
returned to the orchestrator.

The Langfuse TOOL span for this is `search_child_chunks`. See also [tools-search.md](tools-search.md)
for the parent `tools` CHAIN span.

**Langfuse span:** TOOL `search_child_chunks`

---

## Symptoms

- `num_results=0` in `qa.jsonl` even though relevant PDFs are in the knowledge base
- Retrieved chunks are from the wrong document
- Suspiciously uniform answer quality degradation after a re-index operation
- Search latency > 1 s (baseline < 0.15 s)

---

## Debug Commands

### 1. Fleet history — search latency

```bash
.venv/bin/python -m debug.analyze --node search_child_chunks
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: search_child_chunks  (n=61)
  latency (s): p50=0.09  p90=0.11  p95=0.11  max=0.14  min=0.07
  errors/warnings: 0
```

**Baseline:** max=0.14 s across 61 searches. Any value > 1 s = Qdrant connectivity issue.

### 2. Inspect what was retrieved for a specific query

```bash
.venv/bin/python -m debug.pipeline a687e093
```

Real output (excerpt):

```
────────────────────────────────────────────────────────────
  [SEARCH #1]  0.09s
      query  : {'query': '교내 학생식당 오늘 점심 메뉴', 'limit': 7}
      output : Parent ID: 2026학년도1학기학사안내_parent_14
File Name: 2026학년도1학기학사안내.pdf
Content: ##  2021학번
...
```

The retrieved chunk is from `2026학년도1학기학사안내.pdf`, which contains academic policy —
not cafeteria menus. This is correct behavior: the KB does not contain menu data, so
the search returns the closest match it can find.

### 3. Threshold sensitivity check

Use the repro toolkit to vary the score threshold and see what changes:

```bash
.venv/bin/python -m debug.repro search '수강신청은 어떻게 하나요?' --threshold 0.1
.venv/bin/python -m debug.repro search '수강신청은 어떻게 하나요?' --threshold 0.3
```

Compare result counts at each threshold. If the right chunk appears at 0.1 but not 0.3,
the default threshold is filtering it out.

### 4. app.log grep

```bash
.venv/bin/python -m debug.logs <tid>
```

Check `results=<N>` in the `[chat-OUT]` line:

```
[chat-OUT] tid=a687e093 ... answer_chars=21511 results=4 sources=1 total_ms=290452
```

`results=4` = 4 chunks returned. `sources=1` = all from one PDF.

### 5. qa.jsonl lookup

```bash
.venv/bin/python -m debug.logs <tid>
```

Check `"num_results"` and `"sources"`:

```json
{
  "trace_id": "a687e093",
  "num_results": 4,
  "sources": ["2026학년도1학기학사안내.pdf"],
  "tool_calls": 1
}
```

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Expand **`tools`** → **`search_child_chunks`**
3. Check `input`: the query string sent to Qdrant
4. Check `output`: the raw chunk texts and their source filenames
5. Count how many distinct source documents appear

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **No results** | `num_results=0`, `tool_calls≥1` | Check Qdrant health; verify collection exists |
| **Wrong document retrieved** | Sources show irrelevant PDFs | Inspect embedding quality with `repro search`; check if KB was re-indexed with different chunking |
| **Score threshold too aggressive** | Good chunk at score 0.25, threshold at 0.3 | Lower threshold in config; test with `repro search --threshold 0.2` |
| **Qdrant latency spike** | `search_child_chunks` > 1 s | Check Qdrant container memory/CPU; restart if needed |
| **Stale index** | Answer quality degrades after new PDF added | Re-index with `eval_tools/indexer.py`; verify `kb_docs` count in `/health` response |

### Checking KB doc count

```bash
curl http://localhost:8000/health | python3 -m json.tool | grep kb_docs
```

Expected: `"kb_docs": 68` (or whatever the current indexed count is).
If 0 or unexpectedly low, the index is empty or stale.
