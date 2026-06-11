# Module: orchestrator

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`orchestrator` is the LangGraph agent loop that decides when to call tools (search)
and when to stop. Each tool-call round trips through orchestrator → tools →
search_child_chunks. **Multiple orchestrator observations per trace** means multiple
tool-call rounds (agent loop).

**Langfuse span:** CHAIN `orchestrator` (one span per tool-call round)

---

## Symptoms

- Total latency >> expected, but `aggregate_answers` is fast
- PIPELINE_TIMING `agent=<large>ms` with `tool_calls=4–8` in `qa.jsonl`
- Langfuse shows 4–8 `orchestrator` spans on the same trace ID
- Answer contains repetitive sub-question results

---

## Debug Commands

### 1. Fleet history — loop depth

```bash
.venv/bin/python -m debug.analyze --node orchestrator
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations

======================================================================
NODE: orchestrator  (n=119)
  latency (s): p50=2.95  p90=10.47  p95=11.88  max=13.56  min=1.38
  errors/warnings: 0

  Recent 20 executions:
    2026-06-08T11:29:46  tid=f1653782  lat=5.90s  tok=-
    2026-06-08T11:29:42  tid=f1653782  lat=3.69s  tok=-
    2026-06-08T11:29:40  tid=e8fb81e8  lat=3.91s  tok=-
    2026-06-08T11:29:39  tid=e8fb81e8  lat=1.49s  tok=-
    2026-06-08T11:22:15  tid=091e75b9  lat=11.22s  tok=-
    2026-06-08T11:22:04  tid=091e75b9  lat=10.47s  tok=-
    2026-06-08T11:22:03  tid=1992debf  lat=10.75s  tok=-
    2026-06-08T11:22:01  tid=1992debf  lat=8.27s  tok=-
    2026-06-08T11:21:58  tid=1992debf  lat=4.82s  tok=-
    2026-06-08T11:21:58  tid=1992debf  lat=3.37s  tok=-
    2026-06-08T11:16:29  tid=13d9d8d8  lat=11.40s  tok=-
    2026-06-08T11:16:28  tid=13d9d8d8  lat=9.48s  tok=-
    2026-06-08T11:16:27  tid=13d9d8d8  lat=5.66s  tok=-
    2026-06-08T11:16:25  tid=13d9d8d8  lat=1.51s  tok=-
```

Multiple rows per tid = multiple tool-call rounds:
- `f1653782`: 2 rounds (2 × orchestrator), normal
- `1992debf`: 4 rounds (4 × orchestrator), elevated
- `13d9d8d8`: 4 rounds, each slow (5–11 s each)

### 2. Fleet loop-depth distribution

```bash
.venv/bin/python -m debug.analyze
```

Real output (excerpt):

```
LLM CALLS PER TRACE (agent-loop depth):
  mean=4.4  p50=4  p90=6  max=9
  distribution: {1: 1, 2: 2, 4: 32, 5: 10, 6: 6, 7: 1, 9: 1}
```

`max=9` = the agent-loop blowup case (see Known Failure Modes).

### 3. Single-trace drilldown

```bash
.venv/bin/python -m debug.pipeline <tid>
```

Pipeline shows one `[SEARCH #N]` per orchestrator round:

```
  [SEARCH #1]  0.09s
      query  : {'query': '...', 'limit': 7}
  [SEARCH #2]  0.10s
      query  : {'query': '...', 'limit': 7}
  ...
```

For an agent-loop blowup trace (7f37cac8 = 144 s, 8 tool calls):

```bash
.venv/bin/python -m debug.pipeline 7f37cac8
```

Expect 8 `[SEARCH #N]` blocks with `ANOMALY FLAGS: ⚠ AGENT LOOP: 8 tool calls`.

### 4. app.log grep

```bash
.venv/bin/python -m debug.logs 7f37cac8
```

Look for `tool_calls=8` in PIPELINE_TIMING:

```
2026-06-05 15:41:34,406 [7f37cac8] INFO api.chat:_finalize:41 - PIPELINE_TIMING
    tid=7f37cac8 total=149718ms summarize=0ms rewrite=1764ms agent=143719ms
    aggregate=4234ms other=0ms sub_q=1 tool_calls=8
```

`agent=143719ms, tool_calls=8` = agent-loop blowup. `aggregate=4234ms` = fast once out of loop.

> **KST/UTC note:** PIPELINE_TIMING timestamp = KST. Langfuse span start = UTC (subtract 9h).

### 5. qa.jsonl lookup

```bash
.venv/bin/python -m debug.logs 7f37cac8
```

Check `"tool_calls"` field:

```json
{
  "trace_id": "7f37cac8",
  "duration_ms": 149718,
  "num_results": 9,
  "tool_calls": 8,
  "timing": {
    "agent": 143719,
    "aggregate_answers": 4234
  }
}
```

---

## Langfuse Span to Open

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter `metadata.trace_id = <tid>`
3. Count the **`orchestrator`** spans under the `agent` parent
4. Each span's `output` shows the tool call decision (search query or FINISH)
5. High-latency spans indicate slow Ollama decisions under heavy context

---

## Known Failure Modes

| Failure | Signal | Values seen |
|---------|--------|-------------|
| **Agent-loop blowup** | `tool_calls=8`, `agent=143–166 s` | 7f37cac8: 144 s, 8 calls |
| **Elevated loop rounds** | `tool_calls=4–6`, `agent=25–50 s` | 1992debf, 13d9d8d8: 4 rounds each |
| **Slow single round** | One orchestrator span > 11 s | Ollama context pressure |

### Agent-loop blowup: what to do next

The model issued 8 search calls (the loop limit). This happens on complex multi-part
questions where the model repeatedly retrieves and discards results.

```bash
# Inspect the exact agent loop, span by span:
.venv/bin/python -m debug.pipeline 7f37cac8

# Re-run the full graph on the same question (non-deterministic, prod box only):
.venv/bin/python -m debug.repro answer "<the question from the trace>"
```

Mitigation: lower the `MAX_TOOL_CALLS` env var (`project/config.py:75`, read from
`project/.env`), or improve the rewrite prompt to generate simpler,
single-intent queries.
