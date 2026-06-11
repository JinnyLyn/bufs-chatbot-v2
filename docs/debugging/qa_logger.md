# Module: qa_logger

> **Timezone note:** `qa.jsonl` timestamps are **KST (local time, `datetime.now()`)** —
> same as `app.log`. Only Langfuse uses UTC.

## Overview

`qa_logger` is a local file-write component — it appends one JSON record to
`logs/qa/qa_YYYY-MM-DD.jsonl` after each completed request. There is **no Langfuse
span** for this module; it runs entirely on the production box after the HTTP
response is sent.

A write failure is silent in Langfuse. The only signals are:
1. Missing `qa.jsonl` record for a trace where `app.log` shows `[chat-OUT]`
2. A `Q&A log write failed:` ERROR line in `app.log` (`qa_logger.py:78`;
   0 real occurrences in production history)

---

## Symptoms

- `debug.logs <tid>` shows `(no QA record found)` but `app.log` has both chat-IN and chat-OUT
- `logs/qa/qa_*.jsonl` file is absent or empty
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

Two distinct failure messages exist (verify both):

```bash
# realistic path: QALogger.log() swallows the write error internally and
# logs at ERROR (qa_logger.py:78)
grep "Q&A log write failed" logs/backend/app.log*

# wrapper path: only if log() itself raised before writing (chat.py:57, WARNING)
grep "Q&A log failed" logs/backend/app.log*
```

Neither line has appeared in real production logs so far (0 occurrences).
If you see one, the app logged the exception and continued — the request
itself completed normally.

> Known gap: `debug.logs <tid>` currently parses only INFO/WARNING lines, so the
> ERROR-level "Q&A log write failed" line would not appear in its output — use
> the `grep` above directly. Tracked in the debug-toolkit code-bug issue.

### 3. Verify qa.jsonl files exist

```bash
ls -lh logs/qa/qa_*.jsonl 2>/dev/null || echo "no qa files found"
```

If absent, QA logging may be disabled (`CHAT_LOG_DISABLED` env, or per-request
`X-Test-Mode` header) or `config.LOG_DIR` points elsewhere. The directory is
`<config.LOG_DIR>/qa/` (`qa_logger.py:19`).

### 4. Cross-reference chat-OUT count vs QA record count

```bash
grep -h '\[chat-OUT\]' logs/backend/app.log* | wc -l   # only the retained rotated days (LOG_BACKUP_DAYS, default 30)
cat logs/qa/qa_*.jsonl | wc -l                          # qa files are never pruned — span ALL days
```

These counts only line up while the server is younger than the app.log
retention window (`LOG_BACKUP_DAYS`, default 30 — `log_setup.py:38`); qa files
are never rotated away, so on an older deployment qa will exceed chat-OUT and
you should compare per-day instead (e.g. `app.log.2026-06-08` vs
`qa_2026-06-08.jsonl`). Test-mode requests skip the QA record but still log
chat-IN/chat-OUT — the `test=True` marker is on the **[chat-IN]** line
(chat-OUT has no test field), so exclude them by matching tids from
`grep 'chat-IN.*test=True'`. After accounting for both, chat-OUT entries
exceeding same-window QA records mean some requests completed without a QA
write.

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
| **Missing QA record** | `(no QA record found)` in `debug.logs`, but chat-OUT exists | Check disk space; check `CHAT_LOG_DISABLED` / `X-Test-Mode`; grep for `Q&A log write failed` in app.log |
| **Disk full** | `Q&A log write failed: ... No space left on device` (ERROR) | Clear old rotated logs; expand disk |
| **Write race** | Two concurrent requests write to same qa file | Not observed so far. Append mode is atomic per write() syscall, but very long answers (>~8 KB buffered) can flush as multiple syscalls and interleave; a corrupted line is silently skipped by `_parse` — would surface as a missing QA record with chat-OUT present |

Notes:
- Answers are stored **in full** — there is no truncation (the 21,511-char
  `a687e093` answer is stored whole). A record that ends mid-sentence indicates
  the generation itself stopped, not a qa_logger limit.
- QA write failure has never appeared in real production logs as of 2026-06-10;
  coverage exists only via synthetic test fixtures.
