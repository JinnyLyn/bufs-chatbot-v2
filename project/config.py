import os

# The Ollama *client* must dial a real address. When OLLAMA_HOST is a bind-all
# address (0.0.0.0 — set so the Ollama *server* listens on every interface), the
# client would try to connect to 0.0.0.0 and fail with WinError 10049. Rewrite it
# to loopback for outgoing connections.
_ollama_host = os.environ.get("OLLAMA_HOST", "")
if _ollama_host.startswith("0.0.0.0"):
    os.environ["OLLAMA_HOST"] = _ollama_host.replace("0.0.0.0", "127.0.0.1", 1)

# --- Directory Configuration ---
_BASE_DIR = os.path.dirname(os.path.dirname(__file__))

MARKDOWN_DIR = os.path.join(_BASE_DIR, "markdown_docs")
PARENT_STORE_PATH = os.path.join(_BASE_DIR, "parent_store")
QDRANT_DB_PATH = os.path.join(_BASE_DIR, "qdrant_db")

# --- Logging / Observability ---
# Persistent logs live under <repo>/logs (backend app log + per-day Q&A JSONL).
LOG_DIR = os.path.join(_BASE_DIR, "logs")
LOG_BACKUP_DAYS = int(os.environ.get("LOG_BACKUP_DAYS", "30"))
# When set (env or X-Test-Mode header), Q&A JSONL logging is skipped — used by
# eval/regression runners so they don't pollute production logs.
CHAT_LOG_DISABLED = os.environ.get("CHAT_LOG_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}

# --- Qdrant Configuration ---
CHILD_COLLECTION = "document_child_chunks"
SPARSE_VECTOR_NAME = "sparse"

# --- Model Configuration ---
# bge-m3 is multilingual (handles Korean well, unlike the English-only mpnet default).
# Override with the DENSE_MODEL env var if you want a different embedding model.
# NOTE: changing this changes the vector dimension — re-ingest (ingest.py --clear)
# after switching, since an existing Qdrant collection keeps its original size.
DENSE_MODEL = os.environ.get("DENSE_MODEL", "BAAI/bge-m3")
SPARSE_MODEL = "Qdrant/bm25"
# Run the embedding model on CPU by default so the GPU's VRAM stays free for the LLM
# (query embedding is a single short forward pass — fast enough on CPU). Set
# EMBEDDING_DEVICE=cuda to use the GPU instead.
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "cpu")
# Ollama model for the agent. Must support tool-calling. The default is a small,
# fast, *non-thinking* instruct model; override with LLM_MODEL to use one you have
# pulled (e.g. "qwen3:8b"). Avoid "thinking" models — their reasoning tokens leak
# into the streamed answer.
# Explicit Ollama server URL for the LLM client (overrides OLLAMA_HOST). Point this at a
# LOCAL Ollama (e.g. http://127.0.0.1:11435) instead of an OLLAMA_HOST that SSH-tunnels to
# a remote box. Empty = use OLLAMA_HOST / default.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))
# Disable "thinking" mode by default: on Qwen3-family models the reasoning tokens
# otherwise leak into the streamed answer and slow generation down. Set
# LLM_REASONING=true to re-enable.
LLM_REASONING = os.environ.get("LLM_REASONING", "false").lower() in ("1", "true", "yes", "on")
# Context window. Qwen3.5 advertises a 256K context; if Ollama loads it at that size
# the KV cache balloons (~18 GB) and spills off a 12 GB GPU onto the CPU. Cap it so the
# model fits in VRAM and runs on the GPU. Raise only if you have spare VRAM.
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "8192"))
# Structured-output method for rewrite_query. Models differ: qwen3.5:9b only works via
# "function_calling"; qwen3:4b-instruct only via "json_schema"/default. "auto" tries them
# in order and uses the first that returns a valid object. Pin one to skip the fallback.
STRUCTURED_OUTPUT_METHOD = os.environ.get("STRUCTURED_OUTPUT_METHOD", "auto")

# --- Retrieval Configuration ---
# Score cutoff for child-chunk search. NOTE: with hybrid (dense+sparse) retrieval,
# Qdrant returns RRF *rank* scores (top hit ≈ 0.5, then 0.33, 0.25, …), NOT cosine
# similarity — so the original 0.7 default rejected everything on a real corpus.
# 0.3 keeps roughly the top ~4 ranked hits; lower it toward 0.0 to keep more.
SEARCH_SCORE_THRESHOLD = float(os.environ.get("SEARCH_SCORE_THRESHOLD", "0.3"))

# --- Agent Configuration ---
# Caps on the orchestrator loop. Lower = faster (fewer sequential LLM calls) but the
# agent searches less thoroughly. env-overridable so they can be tuned / rolled back
# without a code change. Defaults are the original repo values.
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "8"))
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "10"))
GRAPH_RECURSION_LIMIT = 50
# Context size (tokens) above which the agent runs the expensive compress_context node.
# At the default 2000 a single 6000-char parent crosses it, so compression fires after
# EVERY tool call (each ~600-token summary = ~20s) — the dominant latency cost. Raise it
# (with a larger LLM_NUM_CTX) so a normal 3-retrieval turn keeps full context and never
# compresses. env-overridable for tuning/rollback.
BASE_TOKEN_THRESHOLD = int(os.environ.get("BASE_TOKEN_THRESHOLD", "2000"))
TOKEN_GROWTH_FACTOR = 0.9

# --- Text Splitter Configuration ---
CHILD_CHUNK_SIZE = 500
CHILD_CHUNK_OVERLAP = 100
MIN_PARENT_SIZE = 2000
# Larger parents so cohort/requirement tables stay whole in one parent chunk (the chunker
# is table-aware and never splits inside a markdown table). retrieve_parent_chunks then
# hands the LLM the full table, so per-cohort rows (e.g. 2017~2020학번) are extractable.
MAX_PARENT_SIZE = int(os.environ.get("MAX_PARENT_SIZE", "6000"))
HEADERS_TO_SPLIT_ON = [
    ("#", "H1"),
    ("##", "H2"),
    ("###", "H3")
]

# --- Langfuse Observability ---
LANGFUSE_ENABLED = os.environ.get("LANGFUSE_ENABLED", "false").lower() == "true"
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_BASE_URL = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000")
# The Langfuse SDK reads LANGFUSE_HOST (not LANGFUSE_BASE_URL) from the env — mirror it so
# the configured cloud region (EU cloud.langfuse.com vs US us.cloud.langfuse.com) is used.
if LANGFUSE_ENABLED and LANGFUSE_BASE_URL and not os.environ.get("LANGFUSE_HOST"):
    os.environ["LANGFUSE_HOST"] = LANGFUSE_BASE_URL
