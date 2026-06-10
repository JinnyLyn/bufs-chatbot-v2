# Module: fallback_response

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`fallback_response` generates a canned "I don't know" reply when the pipeline determines
it cannot answer the question (e.g., after multiple empty search rounds). In production
this path has never been activated — the pipeline routes to `aggregate_answers` instead,
which generates its own "not found" sentinel text.

**Langfuse span:** CHAIN `fallback_response`

---

## Path Liveness Check

**Run this first** — if the node has zero observations, the path is inactive and you
can skip the rest of this runbook.

```bash
.venv/bin/python -m debug.analyze --node fallback_response
```

Real output (2026-06-10):

```
Pulled 200 traces, 1200 observations
  No observations found for node 'fallback_response'.
  Tip: 'retrieve_parent_chunks', 'compress_context', 'fallback_response' were
  unobserved in 200 production traces — this path may be inactive.
```

**Status: path inactive.** Zero observations across 200 production traces as of 2026-06-10.

Note: what looks like a "fallback" to users (finding nothing in the KB and saying so)
is actually produced by `aggregate_answers` with the sentinel text
`찾지 못했습니다`. That is the active "not found" path. This module (`fallback_response`)
is a separate, unreachable branch in the current graph routing.

---

## Symptoms (if path ever activates)

- Short, generic "I don't know" responses instead of the usual sentinel text
- `fallback_response` span appears in Langfuse for the first time
- No `aggregate_answers` span on the same trace

---

## Debug Commands (when path becomes active)

### 1. Confirm activation

```bash
.venv/bin/python -m debug.analyze --node fallback_response
```

If `n > 0`, note the first trace IDs and inspect:

```bash
.venv/bin/python -m debug.pipeline <first-tid-with-fallback>
```

### 2. Determine what triggered the routing change

The routing is controlled by `route_after_orchestrator_call`. If `fallback_response`
activates, the routing condition changed — either by code change or by a model output
that hit a previously unreachable branch.

Check recent code changes:

```bash
git log --oneline -10 project/core/
```

### 3. app.log grep

```bash
.venv/bin/python -m debug.logs <tid>
```

Look for `results=0` in `[chat-OUT]` — a fallback response typically means no results:

```
[chat-OUT] tid=<tid> ... results=0 total_ms=<fast>
```

A fast total with `results=0` and no `aggregate_answers` span = fallback path activated.

---

## Langfuse Span to Open (when active)

1. Open [cloud.langfuse.com](https://cloud.langfuse.com) → **Traces**
2. Filter `metadata.trace_id = <tid>`
3. Look for **`fallback_response`** span — it replaces `aggregate_answers` in the timeline
4. Check `output.messages[].content` for the fallback text
5. Check the parent `agent` span to see why routing landed here

---

## Known Failure Modes (hypothetical — path not yet observed)

| Failure | Signal | Action |
|---------|--------|--------|
| **Unexpected activation** | `fallback_response` appears after code change | Check `route_after_orchestrator_call` routing logic |
| **Fallback text in wrong language** | English fallback delivered to Korean user | Check fallback prompt template language |

> Since this path has never fired in production, these failure modes are inferred from
> the code, not observed. Update this runbook when the first real activation occurs.
