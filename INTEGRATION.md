# CamChat UI × Agentic-RAG core

This repo fuses two projects:

- **Frontend** — the CamChat (BUFS) **Next.js chat UI** (`frontend/`), reduced to the
  core chat experience.
- **Backend + search→generation pipeline** — the **agentic-RAG** LangGraph core
  (`project/`), exposed over HTTP/SSE.

Auxiliary CamChat features were intentionally **dropped**: admin console, login /
auth, transcript / academic-report analysis, and the quick-shortcut panels.

```
 Next.js chat UI  ──HTTP/SSE──▶  FastAPI (project/server.py)
 (frontend/)                         │
                                     ▼
                         LangGraph agent (project/rag_agent)
                         summarize → rewrite → [clarify] → agent(tools) → aggregate
                                     │
                          Qdrant (hybrid: bge-m3 + BM25) + parent store
```

### How the bridge works
- `POST /api/session` mints a `session_id` that is used directly as the LangGraph
  **thread_id** (so each browser tab is an isolated multi-turn conversation).
- `GET /api/chat/stream` runs the agent in a worker thread and streams Server-Sent
  Events: `token` (answer text), `done` (`{answer, source_urls, results, intent,
  duration_ms}`), `error`. Only the final `aggregate_answers` node's tokens are
  surfaced as the answer; earlier reasoning stays hidden behind the “thinking” UI.
- Retrieved documents are parsed out of the agent's tool output into `results` for
  the Source panel (`project/api/sources.py`).

---

## Prerequisites
- **Ollama** running locally with a tool-capable, *non-thinking* model:
  ```
  ollama pull qwen3:4b-instruct-2507-q4_K_M
  ```
  (Or set `LLM_MODEL` to one you already have, e.g. `qwen3:8b`.)
- **Python 3.12** and **Node 20+**.

> **TLS note (this machine):** Norton AV intercepts HTTPS, so pip/HuggingFace can't
> verify certs against certifi. A CA bundle exported from the Windows trust store
> (`win-ca-bundle.pem`) is used instead. It's referenced by `project/.env`
> (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) and passed to pip via `--cert`.
> Regenerate it any time with the snippet at the bottom of this file.

## 1. Install dependencies
```powershell
# backend (use the CA bundle because of the TLS-scanning AV)
python -m pip install --cert win-ca-bundle.pem -r requirements.txt

# frontend
cd frontend; npm install; cd ..
```

## 2. Ingest documents
The knowledge base starts **empty**. Load PDFs/Markdown (e.g. the BUFS academic PDFs):
```powershell
python project/ingest.py "..\bufs-chatbot\data\pdfs" --clear
```
First run downloads the `bge-m3` embedding model (~2 GB). Qdrant runs **embedded**
(local file) — stop the API server before ingesting; only one process can hold the DB.

운영 중 문서 추가·제거(학기/연도 전환, `markdown_docs/archive/` 방식)는
[KB_MANAGEMENT.md](KB_MANAGEMENT.md) 참조.

## 3. Run the backend
```powershell
python project/server.py          # http://localhost:8000  (docs at /docs)
```

## 4. Run the frontend
```powershell
cd frontend; npm run dev          # http://localhost:3000  → redirects to /ko/chat
```
`frontend/.env.local` already points `NEXT_PUBLIC_API_URL` at `http://localhost:8000`.

---

## Configuration (`project/.env` or environment)
| Var | Default | Notes |
|-----|---------|-------|
| `LLM_MODEL` | `qwen3:4b-instruct-2507-q4_K_M` | Ollama model; must support tools. `project/.env` currently sets `qwen3.5:9b` |
| `LLM_REASONING` | `false` | keep “thinking” off — Qwen3 reasoning tokens otherwise leak into the answer and slow it down |
| `LLM_NUM_CTX` | `8192` | context window. Qwen3.5 defaults to 256K, which balloons the KV cache to ~18 GB and spills off-GPU |
| `DENSE_MODEL` | `BAAI/bge-m3` | embedding model; change ⇒ re-ingest with `--clear` |
| `EMBEDDING_DEVICE` | `cpu` | run embeddings on CPU so the GPU VRAM is reserved for the LLM |
| `SEARCH_SCORE_THRESHOLD` | `0.3` | hybrid RRF rank-score cutoff (top hit ≈ 0.5). The original 0.7 rejected everything on a real corpus |
| `CORS_ORIGINS` | `http://localhost:3000` | comma-separated |
| `PORT` | `8000` | backend port |

Agent caps (in `project/config.py`, not env): `MAX_ITERATIONS` (10), `MAX_TOOL_CALLS` (8).

Model note: Qwen3-family models emit structured output reliably only via tool-calling, so
`rewrite_query` uses `with_structured_output(..., method="function_calling")`.

## Observability & Operations

### Logs (always on)
- **`logs/backend/app.log`** — rotating daily (30 days), every line prefixed with the request
  `[trace_id]`. `grep <trace_id> logs/backend/app.log` reconstructs one request end-to-end.
  Per request: `[chat-IN]` → `PIPELINE_TIMING` → `[chat-OUT]` (errors: `[chat-ERR]`).
- **`logs/qa/qa_YYYY-MM-DD.jsonl`** — one line per answered turn:
  `{timestamp, trace_id, session_id, model, question, answer, duration_ms, num_results, sources, sub_questions, tool_calls, timing}`.
  Analyze with `jq` or `api/qa_logger.py:QALogger().read_all()`. Skipped when `CHAT_LOG_DISABLED=true`
  or the request sends `X-Test-Mode: 1` (eval/regression runs).

### Diagnosing latency — `PIPELINE_TIMING`
Each answer logs a stage breakdown, e.g.:
```
PIPELINE_TIMING tid=5eda47e8 total=6188ms summarize=0ms rewrite=6188ms agent=0ms aggregate=0ms sub_q=0 tool_calls=0
```
`rewrite` = query analysis/rewrite, `agent` = orchestrator + tool loop (per sub-question),
`aggregate` = final synthesis. `sub_q` = how many sub-questions the rewrite fanned out into,
`tool_calls` = retrieval calls. A 2-min answer with large `agent` + high `sub_q`/`tool_calls` ⇒ reduce
`MAX_ITERATIONS`/`MAX_TOOL_CALLS` (config.py) or constrain fan-out; `sub_q=0` ⇒ the query was sent to
clarification instead of answered.

### Health / GPU monitoring
- `GET /health` → `{model, ollama_base_url, num_ctx, embedding_device, langfuse_enabled, kb_docs, uptime_s}`
  — confirms which Ollama is in use (local `:11435` vs the remote tunnel).
- `GET /health/llm` → loaded model(s) + **`gpu_offload_pct`** (100% = fully on the local GPU).
- `scripts/healthcheck.ps1` prints both + frontend status; exits non-zero if anything is down.

### Langfuse tracing (Cloud)
Create a project at [cloud.langfuse.com](https://cloud.langfuse.com), then in `project/.env` set the
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` and `LANGFUSE_ENABLED=true`, and restart. Every turn becomes a
grouped trace (LLM calls, tool calls, node transitions, latency/cost), grouped by `session_id`. The
callback is already wired (`api/runtime.py:build_config`); it stays off and harmless without keys.

### Run / autostart scripts
```powershell
scripts\start-all.ps1          # start local Ollama (:11435) + backend (:8000) + frontend (:3000), logs to logs\
scripts\stop-all.ps1           # stop them (leaves the SSH tunnel :11434 untouched)
scripts\healthcheck.ps1        # probe everything
scripts\register-autostart.ps1 # (run once) auto-start the stack at logon via Task Scheduler
```

## Notes / limitations
- agentic-RAG sources are local filenames (no notice URLs), so the Source panel shows
  document name + snippet; `source_urls` is empty.
- The agent answers from whatever you ingest. For good Korean retrieval keep
  `DENSE_MODEL=BAAI/bge-m3` (the original `all-mpnet-base-v2` is English-only).
- The original Gradio app (`project/app.py`) still works independently.

### Performance
An answer can take **tens of seconds to a few minutes**. The agentic graph makes many
sequential LLM calls: `rewrite_query` may split one question into several sub-questions,
and **each** runs a full agent subgraph (orchestrator + tool loop) before
`aggregate_answers` synthesizes them. Speed is dominated by whether the model is on the GPU.

**GPU / VRAM (the big one).** `qwen3.5:9b` is Q4_K_M ≈ **9.5 GB loaded**. On this 12 GB
RTX 4070, `ollama ps` showed only **26 % GPU / 74 % CPU** → multi-minute replies. The cause
is *other GPU apps* (LM Studio, BlueStacks, Docker Desktop, many Chrome/Edge tabs,
PowerPoint…): under Windows WDDM they reserve VRAM, so even though `nvidia-smi` reports
~9.9 GB “free”, Ollama can only *allocate* ~3.5 GB and offloads ~11/43 layers. Forcing
`num_gpu` just fails to allocate.
- **Fix: close the other GPU apps (especially LM Studio) and restart the backend.** Ollama
  reloads with VRAM truly free and offloads ~42/43 layers → answers in ~20–40 s.
- Check the split anytime with `ollama ps` (want `100% GPU`).
- Or switch to a model that fully fits, e.g. `LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M`.

Other levers: lower `MAX_ITERATIONS` / `MAX_TOOL_CALLS` (e.g. 3/3); constrain
`rewrite_query` to fewer sub-questions.

### Ingestion notes
- Only **PDF/Markdown** are ingested; HWP/XLSX in `bufs-chatbot/data` are skipped.
- All 68 BUFS PDFs ingest cleanly after a fix to the original filename handling
  (`utils.py`): `glob` broke on `[ ]` in names, and `Path.with_suffix()` truncated names
  containing dots (`"1. 공고.pdf"`).

## Regenerating the CA bundle
```python
python -c "import ssl,certifi,base64; \
parts=[open(certifi.where(),encoding='utf-8').read()]; \
parts+=['-----BEGIN CERTIFICATE-----\n'+base64.encodebytes(d).decode().strip()+'\n-----END CERTIFICATE-----\n' \
for s in ('ROOT','CA') for d,e,t in ssl.enum_certificates(s) if e=='x509_asn']; \
open('win-ca-bundle.pem','w',encoding='utf-8').write('\n'.join(parts))"
```
