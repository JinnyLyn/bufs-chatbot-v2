# Module: rewrite_query

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`rewrite_query` calls the LLM (qwen3.5:9b via Ollama) to decompose or clarify the user's
question before embedding search. It is the **first LLM call** in the pipeline and the
primary cold-start bottleneck.

Output: `questionIsClear` bool + rewritten messages list.

**Langfuse span:** CHAIN `rewrite_query`

---

## Symptoms

- First response from the chatbot is slow (> 5 s before any answer appears)
- The retrieved chunks are off-topic relative to the user's question
- PIPELINE_TIMING in `app.log` shows `rewrite=<large>ms` with fast `aggregate=`
- A trace shows `rewrite_query` > 10 s in Langfuse

---

## Debug Commands

### 1. Fleet history — node latency distribution

```bash
.venv/bin/python -m debug.analyze --node rewrite_query
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: rewrite_query  (n=52)
  latency (s): p50=1.91  p90=5.34  p95=11.21  max=43.43  min=1.65
  errors/warnings: 0

  Recent 20 executions:
    2026-06-10T07:55:59  tid=cd5eeb93  lat=43.43s  tok=-
    2026-06-08T11:29:39  tid=f1653782  lat=2.79s  tok=-
    2026-06-08T11:29:37  tid=e8fb81e8  lat=1.66s  tok=-
    2026-06-08T11:22:00  tid=091e75b9  lat=4.43s  tok=-
    2026-06-08T11:21:56  tid=1992debf  lat=2.31s  tok=-
    2026-06-08T11:21:34  tid=81dbbfb6  lat=1.76s  tok=-
    2026-06-08T11:20:47  tid=e55080c1  lat=1.65s  tok=-
    2026-06-08T11:16:42  tid=439a53a6  lat=7.02s  tok=-
    2026-06-08T11:16:20  tid=13d9d8d8  lat=5.34s  tok=-
    2026-06-08T11:16:12  tid=18d8b599  lat=1.73s  tok=-
    2026-06-08T11:15:26  tid=461132c8  lat=1.80s  tok=-
    2026-06-08T11:14:08  tid=cb317573  lat=1.65s  tok=-
    2026-06-08T11:13:32  tid=37d6dfdf  lat=1.78s  tok=-
    2026-06-08T11:13:18  tid=b3db8f49  lat=2.33s  tok=-
    2026-06-08T11:11:53  tid=34a27c84  lat=1.68s  tok=-
    2026-06-08T11:10:30  tid=86e2c51e  lat=3.84s  tok=-
    2026-06-08T11:10:22  tid=54c51942  lat=1.89s  tok=-
    2026-06-08T11:09:45  tid=c6ba0d43  lat=2.05s  tok=-
    2026-06-08T11:08:45  tid=681f5bce  lat=5.33s  tok=-
    2026-06-08T11:08:38  tid=c12fa94e  lat=11.21s  tok=-
```

**Baseline:** p50 ≈ 1.9 s. Outlier `cd5eeb93` = 43.43 s — Ollama cold-start (model was
unloaded). The `max=43.43s` is a cold-start artifact, not a runaway.

### 2. Single-trace drilldown

```bash
.venv/bin/python -m debug.pipeline a687e093
```

Real output (excerpt for `rewrite_query`):

```
────────────────────────────────────────────────────────────
  [REWRITE_QUERY]  1.71s
      output (rewritten query): {'questionIsClear': True, 'messages': [{'content': '',
        'type': 'remove', ...}, ...]}
      ┄ What wrong looks like: rewritten query diverges from original intent → embedding
        search retrieves off-topic chunks. OR: rewrite takes >3s → Ollama cold-start /
        context pressure.
      ┄ Suspect module: rewrite_query (QueryAnalysis LLM call)
```

1.71 s = normal. The `output.messages` shows the rewritten query sent to the vector store.

### 3. app.log grep

```bash
.venv/bin/python -m debug.logs a687e093
```

Look for `PIPELINE_TIMING` — `rewrite=<ms>ms`:

```
2026-06-08 16:30:21,722 [a687e093] INFO api.chat:chat_stream:77 - [chat-IN] tid=a687e093 ...
2026-06-08 16:35:12,165 [a687e093] INFO api.chat:_finalize:41 - PIPELINE_TIMING tid=a687e093
    total=290452ms summarize=0ms rewrite=1718ms agent=4125ms aggregate=284594ms
```

`rewrite=1718ms` = 1.72 s — normal. The blowup here was in `aggregate`, not `rewrite`.

> **KST/UTC note:** chat-IN at `16:30:21 KST` = `07:30:21 UTC` in Langfuse.

### 4. qa.jsonl lookup

```bash
.venv/bin/python -m debug.logs <tid>
```

Check `"timing.rewrite_query"` in the record:

```json
{
  "trace_id": "a4f2878e",
  "timing": {
    "rewrite_query": 4766,
    "agent": 7296,
    "aggregate_answers": 4875
  }
}
```

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter by `metadata.trace_id = <8-hex-tid>` or paste the full 32-hex Langfuse ID
3. Expand the **`rewrite_query`** span
4. Check `output.questionIsClear` — if `false`, the router may have taken a different path
5. Check `output.messages` — the rewritten query is the embedding search input

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **Cold-start latency spike** | First trace after idle > 30 s in `rewrite_query` | Warm Ollama: `curl http://localhost:11434/api/generate -d '{"model":"qwen3.5:9b","prompt":"hi"}'` |
| **Rewrite diverges from intent** | Retrieved chunks off-topic despite good question | Inspect `output.messages` in Langfuse span; compare with original question |
| **Sustained high latency** | p90 > 10 s across multiple traces | Check Ollama context pressure — reduce `num_ctx` or restart Ollama |
