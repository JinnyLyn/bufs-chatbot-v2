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

# KB scope: markdown sources kept OUT of the student index (matched against the .md stem).
# Employer/근로기관-facing docs are out of scope for student academic queries yet, being large,
# dominate retrieval — "국가근로장학금 근로기관 안내자료" alone is 20% of child chunks and is the
# top hit for many unrelated queries (form-field-schema chunks + 국가장학금/국가근로장학금 token
# overlap) while never being a correct answer. Excluding it recovered +6pp retrieval recall on the
# qa100 9b set (#108). Comma-separated env KB_EXCLUDE_SOURCES overrides the default set.
_DEFAULT_EXCLUDED_SOURCES = "2026년도 국가근로장학금 근로기관 안내자료"
KB_EXCLUDE_SOURCES = frozenset(
    s.strip() for s in os.environ.get("KB_EXCLUDE_SOURCES", _DEFAULT_EXCLUDED_SOURCES).split(",") if s.strip()
)

# --- Logging / Observability ---
# Persistent logs live under <repo>/logs (backend app log + per-day Q&A JSONL).
LOG_DIR = os.path.join(_BASE_DIR, "logs")
LOG_BACKUP_DAYS = int(os.environ.get("LOG_BACKUP_DAYS", "30"))
# When set (env or X-Test-Mode header), Q&A JSONL logging is skipped — used by
# eval/regression runners so they don't pollute production logs.
CHAT_LOG_DISABLED = os.environ.get("CHAT_LOG_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}

# --- Qdrant Configuration ---
# CHILD_COLLECTION is env-overridable so sparse-tokenizer A/B variants can each
# live in their own collection (e.g. document_child_chunks__kiwi_idf) without
# clobbering the baseline index.
CHILD_COLLECTION = os.environ.get("CHILD_COLLECTION", "document_child_chunks")
SPARSE_VECTOR_NAME = "sparse"

# --- Model Configuration ---
# bge-m3 is multilingual (handles Korean well, unlike the English-only mpnet default).
# Override with the DENSE_MODEL env var if you want a different embedding model.
# NOTE: changing this changes the vector dimension — re-ingest (ingest.py --clear)
# after switching, since an existing Qdrant collection keeps its original size.
DENSE_MODEL = os.environ.get("DENSE_MODEL", "BAAI/bge-m3")
# Sparse (BM25) model for the hybrid retriever's lexical leg. FastEmbed's "Qdrant/bm25"
# tokenizes Korean on whitespace only, so particles glue to nouns (졸업요건은 != 졸업요건)
# and compounds aren't split — the Korean weak link in the sparse leg. Default is "kiwi":
# BM25 over Kiwi morphemes (see db/korean_sparse.py), which beat bm25/okt/bm42 on the
# combined88 A/B (sparse recall@10 0.537→0.687, e2e strict 69.1%→71.6%) and needs no JVM.
# Other values: "okt" (konlpy/Okt — needs a JDK), "whitespace" (JVM-free control),
# "bm42_kiwi" (BM42 fed Kiwi-presegmented text), or any FastEmbed model id (e.g.
# "Qdrant/bm25", "Qdrant/bm42-all-minilm-l6-v2-attentions") passed to FastEmbedSparse.
SPARSE_MODEL = os.environ.get("SPARSE_MODEL", "kiwi")
# Enable Qdrant's server-side IDF on the sparse vector (modifier="idf"). The BM25 sparse
# vectors are TF-only and need IDF for a proper BM25 score. On by default (the kiwi index
# is built with it). NOTE: only applied at create_collection time — switching it requires
# rebuilding the collection (reindex.py).
SPARSE_IDF = os.environ.get("SPARSE_IDF", "true").lower() in ("1", "true", "yes", "on")
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
# Generation safety caps (Tier-0 latency / runaway fixes).
# num_predict: hard ceiling on output tokens. Ollama's default is unbounded (-1), which let a
# single out-of-scope question generate 21,511 chars over 284 s (greedy temp=0 repetition loop,
# qa 2026-06-08). Real answers top out at ~718 output tokens, so 2048 passes all legit traffic
# and only truncates runaways. NOTE: in langchain-ollama 1.1.0 sampling options are read ONLY
# from the ChatOllama constructor (self.*) — .with_config()/.bind(num_predict=…) do NOT apply
# (they land outside the request `options` dict), so these must be set at construction.
LLM_NUM_PREDICT = int(os.environ.get("LLM_NUM_PREDICT", "2048"))
# repeat_penalty > 1 suppresses the greedy repetition that drives runaway generation.
LLM_REPEAT_PENALTY = float(os.environ.get("LLM_REPEAT_PENALTY", "1.1"))
# keep_alive: how long Ollama keeps the model resident in VRAM after a request. The default
# (~5 min) means the next request after idle pays a cold model reload — the 11.5 s rewrite_query
# and 43 s trace outliers were cold-starts, not runaways. -1 keeps it resident (no reload).
_keep_alive_raw = os.environ.get("LLM_KEEP_ALIVE", "-1")
try:
    LLM_KEEP_ALIVE = int(_keep_alive_raw)  # seconds; -1 = keep resident forever
except ValueError:
    LLM_KEEP_ALIVE = _keep_alive_raw       # duration string, e.g. "10m"
# Warm the model into VRAM at startup (a synchronous invoke in RAGSystem.initialize). Default on
# so the first real request is fast. Disable for CI/tests, or when OLLAMA_BASE_URL may point at a
# blackhole host where the warmup would block on a connect timeout instead of failing fast.
LLM_WARMUP = os.environ.get("LLM_WARMUP", "true").lower() in ("1", "true", "yes", "on")
# Structured-output method for rewrite_query. Models differ: qwen3.5:9b only works via
# "function_calling"; qwen3:4b-instruct only via "json_schema"/default. "auto" tries them
# in order and uses the first that returns a valid object. Pin one to skip the fallback.
STRUCTURED_OUTPUT_METHOD = os.environ.get("STRUCTURED_OUTPUT_METHOD", "auto")
# Query rewriting (rewrite_query node). Issue #15: with bge-m3 + Korean academic terms, LLM
# rewriting can hurt retrieval (term drift / morphology / BM25 surface-form mismatch) and the
# clarify-on-short-query path turns answerable questions into clarification requests.
# DEFAULT OFF (issue #51): on combined88 (kiwi+IDF index) rewrite OFF beat ON on every axis —
# contains 81.5%→85.2%, strict 71.6%→72.8%, ~25% faster. OFF recovered 5 questions (4 lost to
# rewrite term-drift, 1 to a false clarification) and lost only 2. The single-turn benchmark
# doesn't exercise rewrite's design value (follow-up pronoun resolution, multi-question split),
# so the toggle stays — set REWRITE_ENABLED=true to re-enable for multi-turn use.
REWRITE_ENABLED = os.environ.get("REWRITE_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# --- Retrieval Configuration ---
# Score cutoff for child-chunk search. NOTE: with hybrid (dense+sparse) retrieval,
# Qdrant returns RRF *rank* scores (top hit ≈ 0.5, then 0.33, 0.25, …), NOT cosine
# similarity — so the original 0.7 default rejected everything on a real corpus.
# 0.3 keeps roughly the top ~4 ranked hits; lower it toward 0.0 to keep more.
SEARCH_SCORE_THRESHOLD = float(os.environ.get("SEARCH_SCORE_THRESHOLD", "0.3"))

# Split-path retrieval (H2, issue #66). The hybrid retriever normally feeds ONE query
# string to both legs. Issue #66's A/B showed a blanket "preserve original wording"
# rule is net-negative because it forces the dense (bge-m3) leg to give up its synonym
# strength. Split-path instead routes per-channel: the lexical/sparse (Kiwi-BM25) leg —
# which matches on Korean morpheme surface forms — gets the user's ORIGINAL question,
# while the dense leg gets the agent's (possibly reworded/semantic) tool-call query.
# DEFAULT OFF — set SPLIT_PATH_ENABLED=true to enable and A/B. When ON but the original
# and the agent query are identical, the result is byte-identical to the normal hybrid.
SPLIT_PATH_ENABLED = os.environ.get("SPLIT_PATH_ENABLED", "false").lower() in ("1", "true", "yes", "on")

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
