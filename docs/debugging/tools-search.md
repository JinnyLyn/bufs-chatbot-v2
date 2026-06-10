# Module: tools / search_child_chunks

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

The `tools` span wraps the tool-dispatch router. `search_child_chunks` is the TOOL span
that actually executes the vector + BM25 hybrid search against Qdrant and returns ranked
child chunks.

Typical latency: ~0.09 s. This is the fastest node in the pipeline; any spike here
indicates a Qdrant connectivity problem, not an LLM issue.

**Langfuse spans:**
- CHAIN `tools` — tool router
- TOOL `search_child_chunks` — hybrid search execution

---

## Symptoms

- Search latency > 1 s (normally < 0.15 s)
- Relevant chunks absent from results despite good question
- `num_results=0` in `qa.jsonl` for a question that should return results
- Total latency dominated by search (not agent or aggregate)

---

## Debug Commands

### 1. Fleet history — `tools` node

```bash
.venv/bin/python -m debug.analyze --node tools
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: tools  (n=61)
  latency (s): p50=0.09  p90=0.11  p95=0.12  max=0.14  min=0.07
  errors/warnings: 0

  Recent 20 executions:
    2026-06-08T11:29:46  tid=f1653782  lat=0.11s  tok=-
    2026-06-08T11:29:40  tid=e8fb81e8  lat=0.12s  tok=-
    2026-06-08T11:22:15  tid=091e75b9  lat=0.10s  tok=-
    2026-06-08T11:22:03  tid=1992debf  lat=0.14s  tok=-
    2026-06-08T11:22:01  tid=1992debf  lat=0.11s  tok=-
    2026-06-08T11:21:37  tid=81dbbfb6  lat=0.08s  tok=-
    2026-06-08T11:20:50  tid=e55080c1  lat=0.07s  tok=-
```

**Baseline:** max=0.14 s. If you see > 1 s, Qdrant is the first suspect.

### 2. Fleet history — `search_child_chunks` (vector tool itself)

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

Latency profile is identical to `tools` — the router overhead is negligible.

### 3. Single-trace search output

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
      ┄ What wrong looks like: relevant chunk absent from results → embedding mismatch
        (dense bge-m3 / sparse bm25 hybrid fusion). OR: score_threshold=0.3 gate filtered
        the chunk → use `repro search --threshold X`.
      ┄ Suspect module: embedding relevancy / chunking (→ repro search / repro chunk)
```

The `output` field shows the raw chunk text returned by Qdrant. Inspect it to confirm
whether the right document was retrieved.

### 4. app.log grep

```bash
.venv/bin/python -m debug.logs <tid>
```

Check `sub_q=` count in PIPELINE_TIMING for total number of search calls:

```
PIPELINE_TIMING tid=a687e093 total=290452ms ... agent=4125ms aggregate=284594ms sub_q=1 tool_calls=1
```

`sub_q=1 tool_calls=1` = one search round. `tool_calls=8` = agent-loop blowup.

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

`num_results=0` = nothing retrieved; `num_results > 0` with bad answer = retrieval hit
wrong document.

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter `metadata.trace_id = <tid>`
3. Expand **`tools`** → child **`search_child_chunks`**
4. Check `input.query` (the embedding query string) and `output` (returned chunks)
5. If `output` is empty or off-topic, this is a retrieval relevancy issue

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **Off-topic retrieval** | Chunks from wrong document; correct answer would be in a different PDF | `repro search --threshold 0.1 '<query>'` to inspect score distribution |
| **No results** | `num_results=0`, `tool_calls=1` | Check Qdrant health; verify KB is indexed |
| **Qdrant timeout** | `search_child_chunks` latency > 1 s | Restart Qdrant container; verify connection settings |
| **Score threshold too high** | Relevant chunk exists but not returned | `repro search --threshold 0.2` vs `0.3` to find cut-off |

> For threshold tuning and chunk-level inspection, see the **repro** toolkit:
> `python -m debug.repro search '<query>'`
