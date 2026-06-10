# Log fixtures (real production samples)

These fixtures are copied **verbatim** from the committed production logs in
`logs/` (commit `82d2e84` "chore: add logs/ directory (#6)", covering server runs
2026-06-05 .. 2026-06-10: `logs/backend/app.log*` and `logs/qa/qa_*.jsonl`).
They are intended as **offline unit-test fixtures for the Phase D debug-toolkit
parsers** — every line/record shape a parser must handle appears here: startup
lines with `-` trace_id, `[chat-IN]` / `[chat-OUT]` / `PIPELINE_TIMING` triples,
an orphaned `[chat-IN]` with no matching OUT (tid `e9eb99b1`), both distinct
WARNING patterns found in production (HF_TOKEN, `core.observability` Langfuse
init failure), the runtime-dict startup line in both variants
(`langfuse_enabled` False/True), old-format `INFO httpx` noise lines (present
only on 2026-06-05, before the current `log_setup.py` quieted httpx to WARNING),
a `[chat-IN]` whose `q=%r` uses double-quote repr and 80-char truncation
(tid `c12fa94e`, `q_chars=153`), and QA records spanning all four day files
including both `num_results=0` records and the slowest request. **Truncation
note:** in `qa_records.jsonl` the record `trace_id=a687e093` (slowest, 290452 ms)
had its 21,511-char `answer` cut to the first 500 chars + `…[truncated 21k chars]`;
all other records are byte-identical to production. No ERROR lines, `[chat-ERR]`
lines, or multi-line traceback blocks exist anywhere in the committed production
logs, so no such fixture could be included — parsers must not assume they have
real-log coverage for those paths.
