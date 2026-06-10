# Module: qa_logger

> **Timezone note:** `qa.jsonl` timestamps are **UTC** (same as Langfuse).
> `app.log` timestamps are **KST (+09:00)**.

## Overview

`qa_logger` is a local file-write component — it appends one JSON record to `qa_*.jsonl`
after each completed request. There is **no Langfuse span** for this module; it runs
entirely on the production box after the HTTP response is sent.

A write failure is silent in Langfuse. The only signals are:
1. Missing `qa.jsonl` record for a trace where `app.log` shows `[chat-OUT]`
2. A `[QA-write-failure]` line in `app.log` (0 real occurrences in production history —
   only observed in synthetic tests)

---

## Symptoms

- `debug.logs <tid>` shows `(no QA record found)` but `app.log` has both chat-IN and chat-OUT
- `qa.jsonl` file is absent or empty
- QA record exists but `"answer"` field is truncated (> 8192 chars)
- Disk full condition on the production server

---

## Debug Commands

### 1. Look up a QA record for a specific tid

```bash
.venv/bin/python -m debug.logs <tid>
```

Example — normal QA record found:

```
============================================================
 qa.jsonl — tid=a4f2878e
============================================================
{
  "timestamp": "2026-06-05T15:36:51",
  "trace_id": "a4f2878e",
  "session_id": "e887585d-...",
  "model": "qwen3.5:9b",
  "question": "수강신청은 어떻게 하나요?",
  "answer": "수강신청은 학교 포털 사이트(학사정보시스템)에서...",
  "duration_ms": 16937,
  "num_results": 4,
  "sources": ["2026학년도1학기학사안내.pdf"],
  "sub_questions": 1,
  "tool_calls": 1,
  "timing": {
    "summarize_history": 0,
    "rewrite_query": 4766,
    "agent": 7296,
    "aggregate_answers": 4875,
    "other": 0
  }
}
```

Example — missing QA record (orphan or write failure):

```
============================================================
 qa.jsonl — tid=a687e093
============================================================
  (no QA record found — orphaned request or tracing disabled)
```

### 2. Check for QA write failure lines in app.log

```bash
grep "QA-write-failure" logs/backend/app.log
```

This line has **never appeared in real production logs** (0 occurrences). If you see
it, the app is logging the exception but continuing — the request completed normally.

### 3. Check for truncated answers

Long answers (> 8192 chars) may be truncated in `qa.jsonl`. Verify with:

```bash
python3 -c "
import json, sys
for line in open('qa_records.jsonl'):
    r = json.loads(line)
    if len(r.get('answer','')) > 8000:
        print(r['trace_id'], len(r['answer']), 'chars')
"
```

### 4. Verify qa.jsonl files exist

```bash
ls -lh qa_*.jsonl 2>/dev/null || echo "no qa files found"
```

If absent, QA logging may be disabled or the output directory is wrong. Check
`BUFS_QA_LOG_DIR` env var and the startup log (`[chat-IN]` should confirm the log path).

### 5. Cross-reference chat-OUT count vs QA record count

```bash
grep -c '\[chat-OUT\]' logs/backend/app.log
wc -l qa_*.jsonl
```

These counts should match. A gap means some requests completed without a QA write.

---

## Langfuse Span to Open

There is no Langfuse span for `qa_logger`. Instead, use the Langfuse trace to confirm
the request completed, then check `qa.jsonl` locally.

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Confirm trace exists and `output` has a final answer
3. Cross-check: `debug.logs <tid>` should show the matching QA record

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **Missing QA record** | `(no QA record found)` in `debug.logs`, but chat-OUT exists | Check disk space; check `BUFS_QA_LOG_DIR`; look for `[QA-write-failure]` in app.log |
| **Disk full** | `[QA-write-failure]` with `OSError: No space left on device` | Clear old rotated logs; expand disk |
| **Truncated answer** | `"answer"` ends mid-sentence at ~8192 chars | Known: `a687e093` answer is 21511 chars, QA record truncates at config limit |
| **Write race** | Two concurrent requests write to same qa file | Not a known issue; qa_logger uses append mode which is atomic on Linux |

> Note: `[QA-write-failure]` is a **synthetic-only** test scenario. It has never appeared
> in real production logs as of 2026-06-10.
