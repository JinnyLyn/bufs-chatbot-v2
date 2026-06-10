# Module: compress_context

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`compress_context` is an optional pipeline step that trims the assembled context before
passing it to `aggregate_answers`. It is gated by `should_compress_context` (a routing
node). In production this gate has always returned "skip" — the path has never been
activated.

**Langfuse span:** CHAIN `compress_context`

---

## Path Liveness Check

**Run this first** — if the node has zero observations, the path is inactive and you
can skip the rest of this runbook.

```bash
.venv/bin/python -m debug.analyze --node compress_context
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations
  No observations found for node 'compress_context'.
  Tip: 'retrieve_parent_chunks', 'compress_context', 'fallback_response' were
  unobserved in 200 production traces — this path may be inactive.
```

**Status: path inactive.** Zero observations across 200 production traces as of 2026-06-10.

To confirm the gate is always routing around this node:

```bash
.venv/bin/python -m debug.analyze --node should_compress_context
```

`should_compress_context` has n=62 observations (same as search calls), all routing
to "skip compress" — this is expected behavior.

---

## Symptoms (if path ever activates)

- Pipeline latency increases unexpectedly without an agent-loop or runaway-answer cause
- `compress_context` span appears in Langfuse for the first time
- Answer quality drops (over-compressed context loses relevant detail)

---

## Debug Commands (when path becomes active)

### 1. Check if the gate fired

```bash
.venv/bin/python -m debug.analyze --node compress_context
```

If `n > 0`, the path is now active. Note the first trace IDs and inspect:

```bash
.venv/bin/python -m debug.pipeline <first-tid-with-compress>
```

### 2. app.log grep

```bash
.venv/bin/python -m debug.logs <tid>
```

No `compress_context` signal in PIPELINE_TIMING (the field is absent when the path is
skipped). If the path activates, check that total latency increased compared to similar
traces without compress.

---

## Langfuse Span to Open (when active)

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter `metadata.trace_id = <tid>`
3. Look for the **`compress_context`** span — it will appear between `tools` and
   `aggregate_answers`
4. Check `input` (assembled chunks) vs `output` (compressed context) — compare lengths
5. Large input-to-output size reduction = aggressive compression that may lose detail

---

## Known Failure Modes (hypothetical — path not yet observed)

| Failure | Signal | Action |
|---------|--------|--------|
| **Over-compression** | Answer loses important detail present in raw chunks | Raise compression threshold or disable the gate |
| **Compression latency spike** | Extra LLM call adds > 5 s to pipeline | Profile `compress_context` span in Langfuse |

> Since this path has never fired in production, these failure modes are inferred from
> the code, not observed. Update this runbook when the first real activation occurs.
