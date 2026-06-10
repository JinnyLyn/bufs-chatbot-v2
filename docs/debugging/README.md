# BUFS Chatbot v2 — Debug Toolkit

> **Timezone note (read once, remember always):**
> Langfuse timestamps are **UTC**. `app.log` timestamps are **KST (+09:00)**.
> `2026-06-08T07:30:21 UTC` in Langfuse = `2026-06-08 16:30:21 KST` in app.log.

---

## Quickstart (cold start on the production server)

This guide is written for the team admin running on the production box (H100 server).

### 1. Enter the repo

```bash
cd /path/to/bufs-chatbot-v2
git pull origin main
```

### 2. Set up the virtual environment (first time only)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Verify `project/.env`

The debug tools load credentials from `project/.env` automatically.
Required variables:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com     # EU cloud — do NOT change to US
```

See `.env.example` for the full template. Never commit the real `.env`.

### 4. Run the first health check

```bash
.venv/bin/python -m debug.status
```

Expected output when the server is up and healthy:

```
BUFS Server Status Check
============================================================

[✓] /health   ok — model=qwen3.5:9b  langfuse_enabled=True  kb_docs=68

Fetching Langfuse data (recent 7d + prior 7d) …
    Pulled: 200 recent traces, 200 prior, 1200 obs

[✓] latency   recent 7d p50=10.8s  prior 7d p50=11.9s  ratio=0.91

[✓] errors    no non-DEFAULT observations

[✓] node liveness:
    LangGraph                      n=109
    aggregate_answers              n=50
    compress_context               (expected absent — path inactive in production)
    fallback_response              (expected absent — path inactive in production)
    orchestrator                   n=119
    rewrite_query                  n=52
    search_child_chunks            n=61
    summarize_history              n=51
    tools                          n=61

[✓] orphan detection:
    no orphans in last 5 log lines

============================================================
STATUS: OK
```

Exit 0 = healthy. Exit 1 = anomaly. Exit 2 = config error (check `project/.env`).

### 5. Fleet overview

```bash
.venv/bin/python -m debug.analyze
```

Sample output (real, 2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
TRACE LATENCY (s): n=199  p50=10.8  p90=20.3  p95=27.6  max=43.5  min=3.8
  slowest traces:
      43.5s  sess=217ac056  {'messages': [{'content': '안녕', ...  tid=cd5eeb93
      43.3s  sess=15598d27  {'messages': [{'content': '성적 처리 방법과 이의신청 절차 알려줘'  tid=6694121f
      32.6s  sess=e428f6a5  {'messages': [{'content': '기존 규정말고 그냥 2020학번 졸업요건  tid=091e75b9
```

---

## Module Runbooks (index)

| Module | Langfuse span | Runbook |
|--------|--------------|---------|
| `rewrite_query` | CHAIN `rewrite_query` | [rewrite_query.md](rewrite_query.md) |
| `orchestrator` | CHAIN `orchestrator` | [orchestrator.md](orchestrator.md) |
| `tools` / `search_child_chunks` | CHAIN `tools` + TOOL `search_child_chunks` | [tools-search.md](tools-search.md) |
| `aggregate_answers` | CHAIN `aggregate_answers` | [aggregate_answers.md](aggregate_answers.md) |
| `summarize_history` | CHAIN `summarize_history` | [summarize_history.md](summarize_history.md) |
| `vector_db` (search) | TOOL `search_child_chunks` | [vector_db.md](vector_db.md) |
| `qa_logger` | (local only — no Langfuse span) | [qa_logger.md](qa_logger.md) |
| `compress_context` ⚠ inactive | CHAIN `compress_context` | [compress_context.md](compress_context.md) |
| `fallback_response` ⚠ inactive | CHAIN `fallback_response` | [fallback_response.md](fallback_response.md) |
| Server monitor (`debug.status`) | — | [status.md](status.md) |
| Trace → root cause walkthrough | — | [trace-to-root-cause.md](trace-to-root-cause.md) |

---

## Common Reference

### Trace ID formats

| Format | Length | Example | Use |
|--------|--------|---------|-----|
| 8-hex app tid | 8 | `a687e093` | In `app.log`, `qa.jsonl`, user reports |
| Langfuse trace ID | 32 | `51c47a5061f70aa291ce68a70f9407e3` | In Langfuse UI URL |

All `debug/` tools accept both formats — the 8-hex tid is resolved via `metadata.trace_id`.

### Quick command cheat-sheet

```bash
# Fleet health + latency baseline
.venv/bin/python -m debug.status

# Fleet stats + slow traces
.venv/bin/python -m debug.analyze

# Drilldown: single trace pipeline timeline
.venv/bin/python -m debug.pipeline <tid>           # annotated (default)
.venv/bin/python -m debug.pipeline <tid> --raw     # plain timestamps only

# Per-session Q&A history + verdicts
.venv/bin/python -m debug.session <session-uuid-or-tid>

# app.log + qa.jsonl lookup by 8-hex tid
.venv/bin/python -m debug.logs <tid>

# Per-module latency / error history (fleet)
.venv/bin/python -m debug.analyze --node <node-name>
.venv/bin/python -m debug.analyze --list-nodes     # see all node names
.venv/bin/python -m debug.analyze --errors         # non-DEFAULT observations only
```

### Known failure modes (fleet-mined)

| Pattern | Signal | Typical values |
|---------|--------|----------------|
| Agent-loop blowup | `tool_calls=8` in `qa.jsonl`, PIPELINE_TIMING `agent=143-166s` | 144–166s total |
| Runaway answer | `aggregate=284-290s` in PIPELINE_TIMING, `answer_chars=21k` | 131–284s, 10–21k chars |
| Orphan chat-IN | chat-IN with no chat-OUT in `app.log` tail | Only crash/abort signal |
| Refuse | `num_results=0, tool_calls=0` in `qa.jsonl` | Generation without search |
| Sentinel-with-results | `찾지 못했습니다` in answer, `num_results>0` | Generation failure |

---

## Log file locations

```
logs/backend/app.log              # active log (KST timestamps)
logs/backend/app.log.YYYY-MM-DD   # rotated logs
qa_*.jsonl                        # QA records (one per question)
```

Override with `BUFS_LOG_DIR` environment variable:

```bash
BUFS_LOG_DIR=/mnt/server/logs .venv/bin/python -m debug.logs a687e093
```
