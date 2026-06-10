# Trace to Root-Cause: Wrong-Context Answer Investigation

**Scenario:** A user reports "session X gave me a totally wrong answer / answered with context
from the wrong subject / made things up."

This runbook walks you from that report to the exact failing line in the pipeline using
three live data sources (Langfuse, `app.log`, `qa.jsonl`) and the `debug.repro` tool for
module-isolated re-execution.

---

## Five-Stage Attribution → Repro Lever Map

Every wrong answer traces to one of these pipeline stages.
Find the stage, pick its repro lever, re-run with the real failing input.

| Stage | Symptom in pipeline output | Repro lever |
|---|---|---|
| **1. Query rewriting** | Rewritten question is mis-scoped or garbled → retrieval is hunting the wrong topic | `python -m debug.repro rewrite "<original question>"` |
| **2. Embedding** (dense bge-m3 inside HYBRID) | Correct query but top chunks are semantically unrelated | `python -m debug.repro search "<rewritten query>"` |
| **3. Relevancy check** (SEARCH_SCORE_THRESHOLD gate) | Correct docs exist in index but were filtered out by the 0.3 score gate | `python -m debug.repro search "<rewritten query>" --threshold 0.25` — compare PASS/FAIL counts |
| **4. Document reading** (parent retrieval) | Chunk passed retrieval but parent content is wrong/incomplete | `python -m debug.repro parent "<parent_id>"` |
| **5. Chunking** | Parent has the right text but a cohort boundary / table split corrupted the excerpt | `python -m debug.repro chunk "<source.md>"` |
| **6. Aggregation** | Retrieval looks correct but answer is hallucinated, runaway, or contradicts the chunks | `python -m debug.repro answer "<original question>"` *(non-deterministic — see §Fidelity caveats)* |

> **Relevancy-check is stage 3, not a separate system.** The `score_threshold=0.3` gate
> runs *inside* `similarity_search()`. Use `--threshold X` to show each chunk as PASS/FAIL
> against any threshold value. Lower it to see what the model would have retrieved at 0.2.

---

## Investigation Template (6 Steps)

### Step 1 — Identify the session

The user report gives you a session ID, a time window, or a quoted answer fragment.

```bash
# Look up the session in Langfuse
python -m debug.session <session_id_or_8hex_tid>
```

`debug.session` lists every Q&A turn with verdict flags:
- `REFUSE` — fast-refuse path fired (rewrite returned `is_clear=False`)
- `NO_RESULTS` — `num_results=0`, search found nothing
- `SENTINEL+RESULTS` — answer says "not found" but `num_results>0` (generation failure)
- `RUNAWAY` — `answer_chars` or `duration_ms` above threshold
- `ORPHAN` — `chat-IN` with no matching `chat-OUT` (crash/abort)

Identify the **bad turn** and note its `tid` (8-hex trace ID).

---

### Step 2 — Inspect the full pipeline for that turn

```bash
python -m debug.pipeline <tid>
```

Output: stage-by-stage render with annotations ("what wrong looks like here").

```
rewrite_query  →  "오늘 교내 학생식당 점심 메뉴"   [is_clear=True, 1 rewritten Q]
agent          →  search: "오늘 교내 학생식당 점심 메뉴"  4 chunks PASS
                  parent: (none retrieved in this trace)
aggregate      →  21511 chars / 284594ms             ← RUNAWAY — suspect stage 6
```

Cross-check with the local logs:

```bash
python -m debug.logs <tid>
```

This joins `app.log*` + `qa.jsonl` by `tid` and shows the raw IN/OUT/TIMING lines
alongside the QA record.

---

### Step 3 — Attribute the suspect stage

Use the five-stage map above.  The pipeline output annotates each stage with
"what wrong looks like here" pointers.

**Decision heuristic:**

| Observation | Likely stage |
|---|---|
| Rewritten query is off-topic or multi-part for a simple question | Stage 1 (rewrite) |
| `num_results=0` or top chunks clearly wrong topic | Stage 2 (embedding) |
| `num_results=0` but `debug.repro search --threshold 0.15` finds relevant chunks | Stage 3 (relevancy gate too tight) |
| Chunks are topically relevant but contain wrong cohort / wrong year | Stage 5 (chunking) |
| Chunks are correct, answer contradicts or ignores them | Stage 6 (aggregation) |
| `answer` starts correct then rambles for pages | Stage 6 (runaway generation) |

---

### Step 4 — Re-execute the suspect stage with the real failing input

Pull the **exact inputs** from the pipeline output (Step 2), not a paraphrase.

```bash
# Stage 1 — did the rewrite distort the question?
python -m debug.repro rewrite "교내 학생식당의 오늘 점심 메뉴는 무엇인가?"

# Stage 2/3 — did retrieval find the right chunks?  did the threshold cut them?
python -m debug.repro search "오늘 교내 학생식당 점심 메뉴"
python -m debug.repro search "오늘 교내 학생식당 점심 메뉴" --threshold 0.15

# Stage 4 — what does the parent actually say?
python -m debug.repro parent "2026학년도1학기학사안내_parent_3"

# Stage 5 — is the chunking boundary correct for this document?
python -m debug.repro chunk "2026학년도1학기학사안내.md"

# Stage 6 — does the e2e answer reproduce the problem?  (non-deterministic)
python -m debug.repro answer "교내 학생식당의 오늘 점심 메뉴는 무엇인가?"
```

---

### Step 5 — Fix and verify

| Stage | Likely fix | Verification |
|---|---|---|
| Rewrite | Adjust `get_rewrite_query_prompt()` or `STRUCTURED_OUTPUT_METHOD` | `repro rewrite` on same input — check `is_clear` + rewritten questions |
| Embedding | Re-ingest with a different `DENSE_MODEL`; run eval suite | `repro search` — top chunks should match expected doc |
| Relevancy gate | Tune `SEARCH_SCORE_THRESHOLD` (env var) | `repro search --threshold X` — verify intended chunks PASS |
| Parent content | Fix source document + re-ingest | `repro parent` — content should contain correct info |
| Chunking | Fix `DocumentChuncker` params or cohort-boundary logic | `repro chunk` — parent boundaries look correct; run `pytest tests/test_document_chunker.py` |
| Aggregation | Fix `get_aggregation_prompt()` or add fast-refuse guard | `repro answer` (multiple runs); eval gate per PR template |

---

### Step 6 — Gate before merging

```bash
# Unit tests (offline, no Langfuse/Ollama)
pytest -m "not integration"

# Per the PR template: accuracy gate N/A for debug tooling; document the fix
```

---

## Worked Example: tid `a687e093` — 290-Second Runaway Answer

**Report:** "봇이 학생식당 메뉴를 물었는데 엄청 오래 걸리고 이상한 답변이 나왔어요."

### Three-source join

**app.log** (KST):

```
2026-06-08 16:30:21,722 [a687e093] INFO api.chat:chat_stream:77 - [chat-IN] tid=a687e093 sid=fb67d251 q_chars=24 q='교내 학생식당의 오늘 점심 메뉴는 무엇인가?' model=qwen3.5:9b test=False
2026-06-08 16:35:12,165 [a687e093] INFO api.chat:_finalize:36 - [chat-OUT] tid=a687e093 sid=fb67d251 answer_chars=21511 results=4 sources=1 total_ms=290452
2026-06-08 16:35:12,165 [a687e093] INFO api.chat:_finalize:41 - PIPELINE_TIMING tid=a687e093 total=290452ms summarize=0ms rewrite=1718ms agent=4125ms aggregate=284594ms other=0ms sub_q=1 tool_calls=1 model=qwen3.5:9b
```

Join key: `metadata.trace_id = "a687e093"` (8-hex) → matches Langfuse trace
`51c47a5061f70aa2` (07:30:21 UTC = 16:30:21 KST). ⚠️ Langfuse=UTC, logs=KST (+9 h).

**qa.jsonl** (2026-06-08, record 36):

```json
{
  "timestamp": "2026-06-08T16:35:12",
  "trace_id": "a687e093",
  "session_id": "fb67d251-090e-431e-a267-b1a4f14847dc",
  "model": "qwen3.5:9b",
  "intent": "",
  "question": "교내 학생식당의 오늘 점심 메뉴는 무엇인가?",
  "duration_ms": 290452,
  "num_results": 4,
  "sources": ["2026학년도1학기학사안내.pdf"],
  "sub_questions": 1,
  "tool_calls": 1,
  "timing": {
    "summarize_history": 0,
    "rewrite_query": 1718,
    "agent": 4125,
    "aggregate_answers": 284594,
    "other": 0
  }
}
```

**Langfuse** (Cloud EU, `python -m debug.pipeline a687e093`):

```
Trace: 51c47a5061f70aa2  latency=290.438s  metadata.trace_id=a687e093
  rewrite_query    1.718s   is_clear=True  → "오늘 교내 학생식당 점심 메뉴"
  agent            4.125s   1 search call → 4 chunks PASS (score≥0.3)
  aggregate_answers 284.594s  ← HOTSPOT: 97.9% of total latency
```

### Stage attribution

| Stage | Evidence | Verdict |
|---|---|---|
| 1. Rewrite | `rewrite=1.7s`, query looks reasonable | ✓ Normal |
| 2/3. Retrieval | `agent=4.1s`, `num_results=4`, `sources=1` | ⚠️ 4 chunks from학사안내 — **wrong document for a cafeteria question** |
| 6. Aggregation | `aggregate=284.6s`, `answer_chars=21511` | ✗ **Root cause: runaway generation** |

The fast-refuse path (`is_clear=False` → clarification request) did NOT fire.
The rewrite returned `is_clear=True` and produced a "search-ready" query from an
out-of-domain question. The agent retrieved 4 chunks from the academic handbook (the
only doc with any lexical overlap to "식당"), and `aggregate_answers` spent 284 seconds
generating a 21,511-character hallucinated answer.

### Repro commands with real outputs

**Stage 1 — rewrite (live vs 4090):**

```
$ python -m debug.repro rewrite "교내 학생식당의 오늘 점심 메뉴는 무엇인가?"

[repro rewrite]
  question : '교내 학생식당의 오늘 점심 메뉴는 무엇인가?'
  ollama   : http://100.91.6.58:11434  model=qwen3.5:9b

Running _invoke_structured_rewrite (QueryAnalysis structured output)…

  is_clear             : True
  rewritten_Q[1]       : 오늘 교내 학생식당 점심 메뉴
```

→ `is_clear=True` confirmed: the rewrite stage did not catch this as an out-of-domain
question.  The question is "clear" by the rewrite model's standards — it simply isn't
in any document we have.

**Stage 2/3 — search fingerprint (production box with torch):**

```
$ python -m debug.repro search "오늘 교내 학생식당 점심 메뉴"

[repro search] index fingerprint: meta=5557288bc54143df sqlite=[18096128B mtime=1781076718] git=152c554
  query     : '오늘 교내 학생식당 점심 메뉴'
  threshold : 0.3  (production default = 0.3)
  k         : 8  (= config.MAX_TOOL_CALLS, matches tools.py:20)

  Running: collection.similarity_search(query, k=8, score_threshold=0.3)

  → 4 chunk(s) PASS (hybrid-fusion score ≥ 0.3):

  [1] PASS  parent_id=2026학년도1학기학사안내_parent_3  source=2026학년도1학기학사안내.pdf
       '...학기 교과목 안내...'…
  ...
```

The 4 chunks that passed are from the academic handbook — not a cafeteria menu source.
The chunks are semantically adjacent to "학교 시설" but contain no actual menu data.
This is correct retrieval behaviour; the **problem is the model hallucinating an answer
from unrelated chunks** rather than saying "I don't know."

**Stage 6 — answer (non-deterministic, see §Fidelity caveats):**

```
$ python -m debug.repro answer "교내 학생식당의 오늘 점심 메뉴는 무엇인가?"

[repro answer]
  NOTE: answer is non-deterministic. The 290 s / 21.5k-char runaway (tid=a687e093,
        Langfuse 51c47a5061f70aa2) may not reproduce on demand.
```

### Root cause summary

`aggregate_answers` received 4 loosely-relevant chunks and was asked to answer an
out-of-domain question ("오늘 학생식당 메뉴"). The LLM correctly started with a disclaimer
but then continued generating for 284 seconds producing 21,511 chars — a **repetition
runaway** inside the `aggregate_answers` LLM call.

**Fix direction:** Add a fast-refuse check **after** rewriting and before retrieval that
detects "question is out-of-domain for an academic-affairs chatbot"
(`num_results=0` after a low-threshold probe, OR semantic distance from the corpus
centroid), and returns a short "죄송합니다. 학사 관련 질문만 도움드릴 수 있습니다." answer immediately.
Alternatively, cap `aggregate_answers` output tokens via `num_predict` in ChatOllama.

---

## Repro Fidelity Caveats

### 1. Index fingerprint — committed vs re-ingested

`repro search` prints a fingerprint on every run:

```
index fingerprint: meta=5557288bc54143df sqlite=[18096128B mtime=1781076718] git=152c554
```

| Component | What it tracks |
|---|---|
| `meta=<hash>` | sha256[:16] of `qdrant_db/meta.json` — collection schema |
| `sqlite=[<size>B mtime=<ts>]` | Raw size and mtime of `storage.sqlite` — changes when re-ingested |
| `git=<sha>` | Last commit SHA that touched `qdrant_db/` — only changes on commit |

**Divergence scenario:** if the production server was re-ingested with new documents
but `qdrant_db/` was not committed to git, the `git=` SHA stays stale while `sqlite=`
changes. Compare the `sqlite=` stat across environments to detect this — the `git=` SHA
alone is not sufficient.

### 2. `--db` escape hatch

By default `repro search` copies `qdrant_db/` to a temp dir to avoid the
process-exclusive lock held by the running server. If you need to search the exact
production index without copying (e.g., after a fresh ingest that wasn't committed):

```bash
# Stop the server first, then:
python -m debug.repro search "<query>" --db /path/to/qdrant_db
```

### 3. "Diagnostic score ≠ production gate value"

`repro search` prints two sets of numbers:

- **PASS/FAIL list** — chunks that cleared `similarity_search(..., score_threshold=0.3)`.
  This is what production sees.
- **`diag_hybrid_score=X.XXXX` block** — from `similarity_search_with_score`.
  Label: **"diagnostic hybrid-fusion score — NOT the production gate value"**.
  These are Reciprocal Rank Fusion (RRF) scores: top hit ≈ 0.5, second ≈ 0.33,
  third ≈ 0.25 … They are **rank-based, not cosine similarity**. The threshold
  filter runs *inside* hybrid fusion; the diag scores and the threshold are not
  directly comparable.

> **Rule of thumb:** a chunk with `diag_hybrid_score=0.28` being labelled `FAIL` against
> `threshold=0.3` is correct — RRF score 0.28 < gate 0.3. Do NOT interpret 0.28 as
> "cosine similarity 28% — nearly relevant." The numbers are not the same scale.

### 4. `repro answer` is non-deterministic

The `aggregate_answers` LLM call uses `temperature=0` but Ollama's greedy decoding
can still diverge across runs (model version, KV-cache state, token order).
The 290 s / 21,511-char runaway in trace `a687e093` may generate a normal-length answer
on a re-run, or a different-length runaway. Do not treat a single `repro answer` run
as a pass/fail signal — compare multiple runs or check for the root-cause fix
(output-token cap or fast-refuse guard) rather than trying to reproduce the exact runaway.

---

## Quick Reference

```bash
# 1. Find the bad turn
python -m debug.session <session_id>

# 2. Full stage trace
python -m debug.pipeline <tid>

# 3. Local log join
python -m debug.logs <tid>

# 4. Repro per stage
python -m debug.repro rewrite "<question>"           # Stage 1
python -m debug.repro search "<query>"               # Stage 2
python -m debug.repro search "<query>" --threshold X # Stage 3
python -m debug.repro parent "<parent_id>"           # Stage 4
python -m debug.repro chunk "<file.md>"              # Stage 5
python -m debug.repro answer "<question>"            # Stage 6 (non-det.)

# Fingerprint (printed automatically on every repro search run)
# meta=<sha> sqlite=[<size>B mtime=<ts>] git=<sha>
```

See `python -m debug.repro --help` for the full environment-requirements matrix
(which subcommands need Ollama / torch / sentence-transformers / app-imports-only).
