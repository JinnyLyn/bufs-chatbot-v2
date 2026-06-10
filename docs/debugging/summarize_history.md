# Module: summarize_history

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`summarize_history` runs at the start of every request. It reads the prior conversation
turns from the session and produces a summary string injected into the rewrite_query
context. For first messages in a session (no history), it returns immediately with
`conversation_summary: ''` — this shows as 0.00 s latency in Langfuse.

**Langfuse span:** CHAIN `summarize_history`

---

## Symptoms

- Follow-up questions answered as if a different topic was discussed
- Summary bleeds in content from a previous user's session
- PIPELINE_TIMING `summarize=<large>ms` (normally 0–7 s)
- Multi-turn sessions where context progressively worsens

---

## Debug Commands

### 1. Fleet history

```bash
.venv/bin/python -m debug.analyze --node summarize_history
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: summarize_history  (n=51)
  latency (s): p50=1.93  p90=4.11  p95=5.20  max=7.66  min=0.00
  errors/warnings: 0

  Recent 20 executions:
    2026-06-10T07:55:59  tid=cd5eeb93  lat=0.00s  tok=-
    2026-06-08T11:29:39  tid=f1653782  lat=0.00s  tok=-
    2026-06-08T11:29:32  tid=e8fb81e8  lat=5.20s  tok=-
    2026-06-08T11:21:57  tid=091e75b9  lat=2.45s  tok=-
    2026-06-08T11:21:53  tid=1992debf  lat=2.38s  tok=-
    2026-06-08T11:21:34  tid=81dbbfb6  lat=-  tok=-
    2026-06-08T11:20:45  tid=e55080c1  lat=1.97s  tok=-
    2026-06-08T11:16:38  tid=439a53a6  lat=3.87s  tok=-
    2026-06-08T11:16:16  tid=13d9d8d8  lat=3.74s  tok=-
    2026-06-08T11:08:30  tid=c12fa94e  lat=7.66s  tok=-
```

**Baseline:** `lat=0.00s` = first message in session (no history, early-exit).
`lat=1.9–7.7s` = LLM summarization of prior turns. `-` = latency not recorded.

The outlier `c12fa94e lat=7.66s` is the longest observed (within normal range).

### 2. Session history inspection

If a user reports wrong context in a multi-turn conversation, inspect the session:

```bash
.venv/bin/python -m debug.session <session-uuid>
```

This lists all Q&A turns in the session with timing and verdicts, so you can see which
turn produced a bad summary.

### 3. Single-trace drilldown

```bash
.venv/bin/python -m debug.pipeline a687e093
```

Real output (excerpt):

```
────────────────────────────────────────────────────────────
  [SUMMARIZE_HISTORY]  0.00s
      output: {'conversation_summary': ''}
      ┄ What wrong looks like: history summary embeds wrong prior-session content →
        follow-up questions answered as if a different topic was discussed.
      ┄ Suspect module: summarize_history (prompt / context-window handling)
```

`conversation_summary: ''` = this was the first message in session fb67d251. Normal.

### 4. app.log grep

```bash
.venv/bin/python -m debug.logs <tid>
```

Look for `summarize=<ms>ms` in PIPELINE_TIMING:

```
PIPELINE_TIMING tid=a687e093 total=290452ms summarize=0ms rewrite=1718ms ...
```

`summarize=0ms` confirms first-message early-exit.

### 5. qa.jsonl lookup

```bash
.venv/bin/python -m debug.logs <tid>
```

Check `"timing.summarize_history"`:

```json
{
  "timing": {
    "summarize_history": 0,
    "rewrite_query": 1718,
    "agent": 4125,
    "aggregate_answers": 284594
  }
}
```

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter `metadata.trace_id = <tid>`
3. Expand the **`summarize_history`** span
4. Check `output.conversation_summary` — empty string = first message, normal
5. Check `input` — contains the full prior conversation turns fed to the summarizer

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **Wrong-session bleedthrough** | Summary contains content from unrelated prior session | Check session isolation logic in `core/session_manager.py`; verify `session_id` uniqueness |
| **Slow summarization** | `summarize=` > 5000 ms in PIPELINE_TIMING | Long conversation history; context window pressure on qwen3.5:9b |
| **Summary causes off-topic rewrite** | `rewrite_query` output diverges after history-heavy sessions | Inspect Langfuse span: `summarize_history.output` → `rewrite_query.input` chain |
