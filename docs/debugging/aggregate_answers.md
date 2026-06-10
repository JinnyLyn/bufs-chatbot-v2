# Module: aggregate_answers

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`aggregate_answers` calls the LLM (qwen3.5:9b via Ollama) with the retrieved chunks to
produce the final Korean answer. This is the most latency-variable node in the pipeline
and the source of **runaway-answer** failures.

**Langfuse span:** CHAIN `aggregate_answers`

---

## Symptoms

- User receives an answer after >30 s total latency
- Answer is very long (>5000 chars) or contains repetitive content
- Answer says `찾지 못했습니다` even though search returned results
- `qa.jsonl` shows `"duration_ms"` >> 15000 with `"num_results" > 0`

---

## Debug Commands

### 1. Fleet history — node latency distribution

```bash
.venv/bin/python -m debug.analyze --node aggregate_answers
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: aggregate_answers  (n=50)
  latency (s): p50=3.93  p90=7.61  p95=10.00  max=11.02  min=0.69
  errors/warnings: 0

  Recent 20 executions:
    2026-06-08T11:29:52  tid=f1653782  lat=4.88s  tok=-
    2026-06-08T11:29:44  tid=e8fb81e8  lat=2.90s  tok=-
    2026-06-08T11:22:26  tid=091e75b9  lat=3.93s  tok=-
    2026-06-08T11:22:14  tid=1992debf  lat=7.05s  tok=-
    2026-06-08T11:21:43  tid=81dbbfb6  lat=4.04s  tok=-
    2026-06-08T11:20:55  tid=e55080c1  lat=4.04s  tok=-
    2026-06-08T11:17:00  tid=439a53a6  lat=7.61s  tok=-
    2026-06-08T11:16:41  tid=13d9d8d8  lat=7.15s  tok=-
    2026-06-08T11:16:19  tid=18d8b599  lat=4.37s  tok=-
    2026-06-08T11:15:36  tid=461132c8  lat=4.80s  tok=-
```

**Baseline:** p50 ≈ 3.9 s. Any single trace >60 s in this node = runaway.

### 2. Runaway trace drilldown — `python -m debug.pipeline`

Use the annotated view for a known-runaway trace (a687e093 = 290 s total):

```bash
.venv/bin/python -m debug.pipeline a687e093
```

Real output (excerpt):

```
========================================================================
PIPELINE INSPECTION
  tid       : a687e093
  langfuse  : 51c47a5061f70aa291ce68a70f9407e3
  session   : fb67d251
  total     : 290.4s (4.8min)
  Timezone  : Langfuse=UTC  app.log=KST(+9)

────────────────────────────────────────────────────────────
  [AGGREGATE_ANSWERS]  284.6s (4.7min)
      answer: {'messages': [{'content': '제공된 자료에서 교내 학생식당의 오늘 점심 메뉴에 대한 구체적인 정보는 찾을 수 없습니다...
      ⚠ RUNAWAY: 284.6s (4.7min) — check for LLM repetition loop
      ┄ What wrong looks like: >60s here → LLM repetition loop producing 10k+ char answer.
      ┄ Suspect module: aggregate_answers (→ repro answer '<question>')

========================================================================
ANOMALY FLAGS:
  ⚠ RUNAWAY: total=290.4s (4.8min)
  ⚠ AGGREGATE BLOWUP: 284.6s (4.7min)
```

### 3. app.log grep

```bash
.venv/bin/python -m debug.logs a687e093
```

Look for the PIPELINE_TIMING line — `aggregate=<ms>ms`:

```
2026-06-08 16:30:21,722 [a687e093] INFO api.chat:chat_stream:77 - [chat-IN] tid=a687e093 ... q='교내 학생식당의 오늘 점심 메뉴는 무엇인가?'
2026-06-08 16:35:12,165 [a687e093] INFO api.chat:_finalize:36 - [chat-OUT] tid=a687e093 ... answer_chars=21511 results=4 total_ms=290452
2026-06-08 16:35:12,165 [a687e093] INFO api.chat:_finalize:41 - PIPELINE_TIMING tid=a687e093 total=290452ms summarize=0ms rewrite=1718ms agent=4125ms aggregate=284594ms other=0ms sub_q=1 tool_calls=1
```

`aggregate=284594ms` = 284.6 s — this confirms the blowup.

> **KST/UTC note:** chat-IN at `16:30:21 KST` = `07:30:21 UTC` in Langfuse.

### 4. qa.jsonl lookup

```bash
.venv/bin/python -m debug.logs a687e093
```

The `qa.jsonl` section shows `"duration_ms"` and `"answer"` length:

```json
{
  "timestamp": "2026-06-08T07:30:21",
  "trace_id": "a687e093",
  "duration_ms": 290452,
  "num_results": 4,
  "answer": "제공된 자료에서 교내 학생식당의 오늘 점심 메뉴에 대한 구체적인 정보는 찾을 수 없습니다..."
}
```

`duration_ms=290452` with `num_results=4` = **sentinel-with-results** (generation failure).

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Search by trace ID `51c47a5061f70aa291ce68a70f9407e3` or filter by `metadata.trace_id = a687e093`
3. Expand the **`aggregate_answers`** span
4. Check `output.messages[].content` for length and repetition
5. Check `latency` — if > 60 s, it's a runaway

---

## Known Failure Modes

| Failure | Signal | Values seen |
|---------|--------|-------------|
| **Runaway answer** | `aggregate=` in PIPELINE_TIMING >> 10 000 ms, `answer_chars` > 10 000 | a687e093: 284.6 s / 21 511 chars |
| **Sentinel-with-results** | `찾지 못했습니다` in answer, `num_results > 0` | Generation failure despite retrieved docs |
| **Slow but not runaway** | p90=7.6 s — expected under context pressure | Not an anomaly unless > 30 s |

### Runaway: what to do next

```bash
# Reproduce the exact generation:
.venv/bin/python -m debug.repro answer '교내 학생식당의 오늘 점심 메뉴는 무엇인가?'
```

Runaway answers are caused by LLM repetition loops. The model generates a long preamble
and then loops. Known triggers: long context (many chunks), topic outside KB scope.
Check `num_ctx` in `/health` response — values below 8192 increase repetition risk.

### Sentinel-with-results: what to do next

The LLM said "not found" but the vector search did return results. The retrieved chunks
were likely irrelevant to the question (menu data not in KB). The answer is technically
correct, but the response style may confuse users. Not a pipeline bug.
