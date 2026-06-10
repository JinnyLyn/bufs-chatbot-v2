# Module: Server Monitor (debug.status)

> **Timezone note:** Langfuse timestamps = **UTC**. `app.log` timestamps = **KST (+09:00)**.

## Overview

`debug.status` is the single-shot health monitor. It checks:
1. `/health` endpoint — liveness + runtime config
2. Langfuse latency baseline — rolling 7d p50 vs prior 7d (ratio > 1.5 = DEGRADED)
3. Langfuse non-DEFAULT observations — any error-level spans
4. Pipeline node liveness — expected/unexpected/absent nodes
5. `app.log` orphan detection — chat-IN without chat-OUT (crash/abort signal)

**Exit codes:** 0 = OK, 1 = anomaly, 2 = config error.

---

## Symptoms

- Server is slow or unresponsive
- Users report errors or timeouts
- Latency has been climbing over several days
- Suspected crash (no `[chat-OUT]` for a recent request)

---

## Debug Commands

### 1. Run a full check

```bash
.venv/bin/python -m debug.status
```

Real output (2026-06-10, server not running locally — this is what anomaly looks like):

```
BUFS Server Status Check
============================================================

[✗] /health   connection refused (http://localhost:8000/health)

Fetching Langfuse data (recent 7d + prior 7d) …
    Pulled: 200 recent traces, 200 prior, 1200 obs

[✓] latency   recent 7d p50=10.8s  prior 7d p50=11.9s  ratio=0.91

[✓] errors    no non-DEFAULT observations

[✓] node liveness:
    LangGraph                      n=109
    agent                          n=57
    aggregate_answers              n=50
    collect_answer                 n=61
    compress_context               (expected absent — path inactive in production)
    fallback_response              (expected absent — path inactive in production)
    orchestrator                   n=119
    retrieve_parent_chunks         ⚠ UNEXPECTED (was 0 in 200 production traces)
    rewrite_query                  n=52
    search_child_chunks            n=61
    summarize_history              n=51
    tools                          n=61

[✓] orphan detection:
    no orphans in last 5 log lines

============================================================
STATUS: ANOMALY (1 issue(s))
  ⚠ /health: connection refused (http://localhost:8000/health)

Exit code 1 — trigger alert delivery (see --help for cron/Task Scheduler examples)
```

### 2. Point at a different server URL

```bash
BUFS_SERVER_URL=http://100.91.6.58:8000 .venv/bin/python -m debug.status
# or
.venv/bin/python -m debug.status --server-url http://100.91.6.58:8000
```

### 3. Check orphans manually (app.log tail)

```bash
.venv/bin/python -m debug.logs <tid>
```

An orphan is a `[chat-IN]` with no matching `[chat-OUT]`. This is the **only crash
signal** in `app.log` — if the server crashed mid-request, only chat-IN was written.

### 4. Check latency regression details

```bash
.venv/bin/python -m debug.analyze
```

The fleet stats show p50/p90/p95 and the slowest traces. Compare with the rolling
7d baseline from `debug.status` to identify when the regression started.

---

## Alert Delivery

### Linux — cron

Add to crontab (`crontab -e`) to run every 5 minutes:

```cron
*/5 * * * * cd /path/to/bufs-chatbot-v2 && .venv/bin/python -m debug.status >> /var/log/bufs-status.log 2>&1
```

For Slack notification on failure:

```cron
*/5 * * * * cd /path/to/bufs-chatbot-v2 && .venv/bin/python -m debug.status || \
  curl -s -X POST https://hooks.slack.com/services/YOUR/WEBHOOK/URL \
    -H 'Content-type: application/json' \
    -d '{"text":"BUFS status anomaly — check server"}'
```

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task**
2. **Triggers** tab → **New** → set schedule (e.g. every 5 minutes)
3. **Actions** tab → add **Action 1** (primary check):
   - Program: `C:\path\to\bufs-chatbot-v2\.venv\Scripts\python.exe`
   - Arguments: `-m debug.status`
   - Start in: `C:\path\to\bufs-chatbot-v2`
4. **Actions** tab → add **Action 2** (alert on failure):

   **Option A — Slack webhook:**
   - Program: `powershell.exe`
   - Arguments:
     ```
     -Command "Invoke-WebRequest -Uri 'https://hooks.slack.com/services/YOUR/WEBHOOK/URL' -Method POST -Body '{\"text\":\"BUFS status anomaly\"}' -ContentType 'application/json'"
     ```

   **Option B — email via Send-MailMessage:**
   - Program: `powershell.exe`
   - Arguments:
     ```
     -Command "Send-MailMessage -To 'admin@example.com' -From 'bufs-monitor@example.com' -Subject 'BUFS status anomaly' -SmtpServer 'smtp.example.com'"
     ```

5. Under **Settings**, set "Stop the task if it runs longer than: 1 minute"
6. Under **Conditions**, check "Start only if the network is available"

> Tip: Action 2 only runs when Action 1 exits non-zero **if** you configure it as a
> separate task triggered by the "On an event" trigger watching for exit code 1.
> Simpler: wrap both in a `.ps1` script:
>
> ```powershell
> # bufs-monitor.ps1
> & "C:\..\.venv\Scripts\python.exe" -m debug.status
> if ($LASTEXITCODE -ne 0) {
>     Invoke-WebRequest -Uri 'https://hooks.slack.com/...' `
>         -Method POST `
>         -Body '{"text":"BUFS status anomaly"}' `
>         -ContentType 'application/json'
> }
> ```
>
> Point the Task Scheduler action at `powershell.exe -File C:\...\bufs-monitor.ps1`.

---

## Langfuse Span to Open

`debug.status` uses Langfuse REST API directly — no Langfuse span is created.
For anomaly investigation after a status alert, start with:

```bash
.venv/bin/python -m debug.analyze          # fleet overview
.venv/bin/python -m debug.analyze --errors # non-DEFAULT observations
```

---

## Known Failure Modes

| Failure | Signal | Action |
|---------|--------|--------|
| **`/health` connection refused** | Server not running | Check process: `ps aux \| grep uvicorn`; restart: `bash start.sh` |
| **Latency DEGRADED** | `ratio > 1.5` (recent p50 / prior p50) | Run `debug.analyze` to find slow traces; check Ollama load |
| **Orphan detected** | `⚠ ORPHAN(S)` in status output | Server crashed mid-request; check full `app.log` for exception traceback |
| **Config error (exit 2)** | `Missing LANGFUSE_PUBLIC_KEY` | Check `project/.env`; run `grep pk-lf project/.env` |
| **Unexpected node** | `⚠ UNEXPECTED: retrieve_parent_chunks` | New code path activated; verify intentional; update liveness baselines in `debug/status.py` |
