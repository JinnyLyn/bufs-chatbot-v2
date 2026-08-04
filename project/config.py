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

# --- User accounts (로그인/회원가입) ---
# 계정과 로그인 사용자 질문 이력은 SQLite 파일 하나에 담는다 (Qdrant/parent_store와 별개).
# 개인정보이므로 생성물과 같이 커밋 대상이 아니다 — .gitignore 참조.
USER_DB_PATH = os.environ.get("USER_DB_PATH", os.path.join(_BASE_DIR, "data", "users.db"))
# PBKDF2-SHA256 반복 횟수. OWASP 2024 권장 600k. 테스트에서만 낮춰 잡는다.
USER_PBKDF2_ITERATIONS = int(os.environ.get("USER_PBKDF2_ITERATIONS", "600000"))
# 인증 토큰 서명키. 미지정 시 USER_TOKEN_SECRET_FILE 에 1회 생성·재사용한다
# (하드코딩된 기본 시크릿을 소스에 두지 않기 위함 — api/auth_token.py 참조).
USER_TOKEN_SECRET = os.environ.get("USER_TOKEN_SECRET", "")
USER_TOKEN_SECRET_FILE = os.environ.get(
    "USER_TOKEN_SECRET_FILE", os.path.join(_BASE_DIR, "data", ".user_token.key")
)
USER_TOKEN_TTL_HOURS = int(os.environ.get("USER_TOKEN_TTL_HOURS", "24"))

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
# fast, *non-thinking* instruct model that fits a 12 GB local GPU; override with
# LLM_MODEL to use one you have pulled. Prod / README / .env.example run
# "qwen3.5:9b" via the env var — the small default here is intentional for local
# dev, not the deployed model. Avoid "thinking" models — their reasoning tokens
# leak into the streamed answer.
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

# --- Re-ranking (issue #104: rank_cut 회복 + term-drift 후단 방어) ---
# Cross-encoder rerank over a DEEPER candidate pool. Retrieval fetches RERANK_PREFETCH_K
# candidates at score_threshold 0 (so answer chunks buried at rank 6~20 — below the live
# 0.3 RRF cutoff, our measured rank_cut bucket — are NOT filtered before the reranker sees
# them), a cross-encoder re-scores every candidate against the user's ORIGINAL question
# (state["question"], not the agent's reworded tool-call query), and the top `limit` by
# rerank score are returned. Two wins measured in #104:
#   (a) rank_cut (15 시나리오셋 실측): buried answer chunks promoted into top-5 without
#       lowering the global score_threshold (avoids the W1 latency 회귀 경로).
#   (b) term-drift 후단 방어 (#87, 후보 상한 16/19): split-path puts the original-text answer
#       in the sparse-leg pool but RRF fusion buries it under dense-leg(drift) noise; reranking
#       on the ORIGINAL query surfaces it. Only meaningful with SPLIT_PATH_ENABLED=true.
# DEFAULT OFF — A/B toggle. When ON the reranker is the relevance gate, so the RRF-rank
# score_threshold is bypassed for the prefetch pool (rerank score decides top-k).
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# Candidate pool depth fed to the cross-encoder — 20 matches the #104 probe top-20 window.
RERANK_PREFETCH_K = int(os.environ.get("RERANK_PREFETCH_K", "20"))
# Rerank on CPU by default (same policy as EMBEDDING_DEVICE — keep GPU VRAM for the LLM).
RERANK_DEVICE = os.environ.get("RERANK_DEVICE", EMBEDDING_DEVICE)
# Optional floor on the cross-encoder score below which a candidate is dropped (returns
# NO_RELEVANT_CHUNKS if nothing clears it). Empty = no floor (return top-k unconditionally).
# bge-reranker-v2-m3 emits logits (roughly [-11, +11]); leave off for the first A/B, then
# calibrate against out-of-scope refusal correctness.
_rerank_floor = os.environ.get("RERANK_SCORE_MIN", "").strip()
RERANK_SCORE_MIN = float(_rerank_floor) if _rerank_floor else None
# Score blending: alpha * CE_score_norm + (1-alpha) * RRF_score_norm, before top-k cut.
# Keeps a partial memory of RRF rank so BM25-matched (literal) chunks aren't fully
# overridden by the cross-encoder — addresses the "literal-match 실종" regression class.
# None = pure rerank (alpha=1.0). Candidate values: 0.3 / 0.5 / 0.7 (live sweep pending).
_blend_alpha = os.environ.get("RERANK_BLEND_ALPHA", "").strip()
RERANK_BLEND_ALPHA = float(_blend_alpha) if _blend_alpha else None
if RERANK_BLEND_ALPHA is not None and not (0.0 <= RERANK_BLEND_ALPHA <= 1.0):
    raise ValueError(
        f"RERANK_BLEND_ALPHA must be in [0, 1], got {RERANK_BLEND_ALPHA}. "
        "Values outside this range invert RRF-dominant chunk rankings."
    )

# --- Generation-side lever (issue #145 처방 1: 시나리오형 필수 슬롯 추출) ---
# The 2026-07-20 baseline split (factual100 F1 0.835 vs qa100 F1 0.349, doc_recall 0.792 vs
# answer Recall 0.316) shows scenario questions fail at CONDITION APPLICATION, not just search:
# the agent retrieves the right document yet answers without applying the user's stated
# conditions (학번/신분/휴학 유형 …). When ON, one structured-output call extracts the
# EXPLICITLY-STATED user conditions from the question (never inferred — see UserSlots), and
# the condition block is injected into the orchestrator turns and the final aggregation so
# the answer must apply/flag each condition. Questions with NO stated conditions are
# byte-identical to OFF (no injection), so the factual-question population is untouched —
# the same code-level scoping principle as PR #144's refusal_only gate (prompt-level scoping
# is impossible per PR #111). DEFAULT OFF — A/B on qa100 before enabling.
SLOT_EXTRACTION_ENABLED = os.environ.get("SLOT_EXTRACTION_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# 처방 2 (슬롯-clarify 결합): when the extractor judges that the answer DEPENDS on personal
# conditions the user did not state (UserSlots.required_conditions), the aggregation stage is
# instructed to (a) answer CONDITIONALLY per case from the retrieved content — never dropping
# content (a hard stop-and-ask would zero the answer and repeat issue #51's false-clarification
# regression) — and (b) close with ONE sentence asking for the missing conditions. Aggregation-
# only injection (the sole placement measured safe in the #145 처방-1 ablation: in-loop variants
# regressed refusals/doc_hit). Requires SLOT_EXTRACTION_ENABLED (no-op without it). DEFAULT OFF.
SLOT_CLARIFY_ENABLED = os.environ.get("SLOT_CLARIFY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# 슬롯 기반 2차 검색 (#145 후속, Chunk-문제 타격): CODE-driven supplementary retrieval at the
# aggregation stage — the agent is never asked to search differently (retrieve_parent's 0/100
# tool-choice lesson + the in-loop-injection regressions). Deterministic queries are built from
# the user's STATED slot values ("병역휴학") and the extractor's required-condition NAMES
# ("휴학 유형" — surfaces the very rule tables that differentiate the cases the clarify lever
# asks to split), searched directly against the child collection, and appended to the
# aggregation context as a clearly-labeled supplementary-evidence block. Agent trajectory,
# tool outputs, and the doc_hit metric are untouched by construction; slot-free questions
# stay byte-identical. Requires SLOT_EXTRACTION_ENABLED. DEFAULT OFF — A/B before enabling.
SLOT_SEARCH_ENABLED = os.environ.get("SLOT_SEARCH_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# Per-query top-k and the cap on how many slot-derived queries run (latency guard: each query
# is one CPU embedding pass + a Qdrant lookup — negligible vs an LLM call, but bounded anyway).
SLOT_SEARCH_LIMIT = int(os.environ.get("SLOT_SEARCH_LIMIT", "3"))
SLOT_SEARCH_MAX_QUERIES = int(os.environ.get("SLOT_SEARCH_MAX_QUERIES", "3"))

# --- Generation-side levers (issue #126: 생성실패 40건 법의학 + 시뮬레이션 A/B) ---
# Clean final synthesis. The #126 simulation showed 13/40 of live generation failures pass
# when the SAME child chunks the agent saw are fed to one clean single-shot answer-from-context
# call (fallback prompt, temp0) — i.e. the failure point is the agent loop's multi-turn answer
# synthesis, not the context or the model's extraction ability. When ON, an orchestrator "final
# answer" (no tool calls) is replaced by one clean synthesis call over the collected tool
# evidence (a generalization of the existing fallback path). Routing falls back to the
# orchestrator's own answer when there is no usable tool evidence (e.g. out-of-scope refusals),
# so the refusal path is untouched. DEFAULT OFF — A/B on qa100 before enabling in prod.
CLEAN_SYNTHESIS_ENABLED = os.environ.get("CLEAN_SYNTHESIS_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# Scope of the clean-synthesis rerouting. Live A/B + gemma4:26b judge (PR #144, 2026-07-20)
# measured "always" as accuracy-NEUTRAL (40→39/100): the genfail recoveries (+12) were offset
# by re-synthesis DEGRADING questions the orchestrator draft already answered correctly (−13,
# 10 of them outside the #126 target population). "refusal_only" fires only when the draft
# itself is a refusal ("자료에 없습니다"-class), so every non-refusal draft is kept byte-for-byte
# — the −13 loss class is structurally impossible, keeping only the refusal-recovery upside.
# Unlike prompt-level scoping (PR #111: any prompt insertion reshuffles the whole output
# distribution), a code-level routing condition scopes exactly.
CLEAN_SYNTHESIS_MODE = os.environ.get("CLEAN_SYNTHESIS_MODE", "refusal_only").strip().lower()
if CLEAN_SYNTHESIS_MODE not in ("refusal_only", "always"):
    raise ValueError(
        f"CLEAN_SYNTHESIS_MODE must be 'refusal_only' or 'always', got {CLEAN_SYNTHESIS_MODE!r}"
    )
# Auto parent expansion at the synthesis step. #126 found retrieve_parent_chunks is called
# 0/100 in live runs (the parent-child design's stage 2 is dead in practice) and 9 of the 20
# "same-doc different-chunk" failures have their evidence inside a parent the agent ALREADY saw.
# The naive simulation (parents REPLACING children) went net +4 (8 recovered − 4 regressed on
# needle-in-haystack dilution), so this implements the comment's improved design: KEEP the child
# snippets and APPEND the parent originals (merge, not replace), deduped in first-seen order.
# Expansion happens only in the synthesis/fallback context assembly — the agent loop, its token
# budget, and the compression path are untouched. DEFAULT OFF — A/B toggle.
PARENT_EXPANSION_ENABLED = os.environ.get("PARENT_EXPANSION_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# Caps matching the #126 simulation arms (상한 3개 / 9000자). NOTE: with the default
# LLM_NUM_CTX=8192 a full 9000-char expansion can push the synthesis prompt past the context
# window — when A/B-ing this lever raise LLM_NUM_CTX or lower PARENT_EXPANSION_MAX_CHARS.
PARENT_EXPANSION_MAX_PARENTS = int(os.environ.get("PARENT_EXPANSION_MAX_PARENTS", "3"))
PARENT_EXPANSION_MAX_CHARS = int(os.environ.get("PARENT_EXPANSION_MAX_CHARS", "9000"))

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
