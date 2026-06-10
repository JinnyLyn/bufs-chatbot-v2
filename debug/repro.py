#!/usr/bin/env python3
"""debug/repro.py — Module-isolated re-execution tool.

Subcommands:
    rewrite "<question>"
        Run the real rewrite_query chain (QueryAnalysis structured output) via Ollama.
        Needs: OLLAMA_BASE_URL in project/.env

    search "<query>" [--threshold X] [--db <path>]
        Re-execute the EXACT production search path:
          similarity_search(query, k, score_threshold) over RetrievalMode.HYBRID
          (dense bge-m3 + sparse bm25 — exactly rag_agent/tools.py:20 + db/vector_db_manager.py).
        Prints the index fingerprint on EVERY run.
        PRODUCTION-BOX-ONLY: requires torch + sentence-transformers (not on WSL Python 3.14).

    chunk <md-file>
        Run DocumentChuncker on a markdown file; print parent/child boundaries + sizes.
        App-imports only — works on dev/WSL.

    parent <parent_id>
        Fetch a parent chunk from parent_store/ and print its content.
        App-imports only — works on dev/WSL.

    answer "<question>"
        Single-shot e2e answer via the full RAG graph.
        Non-deterministic — the 290 s / 21.5k-char runaway (trace a687e093) may NOT
        reproduce on demand.  See docs/debugging/trace-to-root-cause.md for context.
        PRODUCTION-BOX-ONLY: requires torch + sentence-transformers + OLLAMA_BASE_URL.

Run `python -m debug.repro --help` or `python -m debug.repro <subcmd> --help`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── sys.path shim: add project/ so app imports (config, rag_agent, db, …) resolve ──
# Same pattern as project/server.py:21.
_WORKTREE = Path(__file__).resolve().parents[1]
_PROJECT = _WORKTREE / "project"
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

# ── dotenv + config: loaded LAZILY via _bootstrap_env(), NEVER at import time ──
# Importing this module must not mutate os.environ (hermetic for pytest);
# config.py computes constants from env at import, so it too is deferred.
from dotenv import load_dotenv as _load_dotenv  # noqa: E402

config = None  # populated by _bootstrap_env(); every cmd_* runs after main() bootstraps


def _bootstrap_env() -> None:
    """Load project/.env then import config — call from main() (or tests that opt in)."""
    global config
    _load_dotenv(_PROJECT / ".env", override=False)
    import config as _config  # deferred so module import stays env-pure

    config = _config

# ── ENV MATRIX (echoed in --help epilog) ─────────────────────────────────────────
_ENV_MATRIX = """\
Subcommand environment requirements:
  rewrite / answer   OLLAMA_BASE_URL  (required) — Ollama server with model pulled
                     LLM_MODEL, LLM_NUM_CTX      — optional overrides
  search             torch, sentence-transformers, qdrant-client
                       → PRODUCTION-BOX-ONLY (not installable on WSL Python 3.14)
                     --db <path>  use an explicit DB path (server must be stopped)
  chunk              app imports only  — works on dev/WSL
  parent             app imports only  — works on dev/WSL

All repro subcommands are integration-tier — never CI-gated.
"""


# ═════════════════════════════════════════════════════════════════════════════
#  Qdrant fingerprint + copy-open
#  These two functions are torch-free and unit-testable on dev/WSL with only
#  qdrant-client installed (T1 venv).
# ═════════════════════════════════════════════════════════════════════════════

def _qdrant_fingerprint(db_path: Path) -> str:
    """Return a one-line fingerprint for the Qdrant index at *db_path*.

    Components
    ----------
    meta=<sha256[:16]>          sha256 of meta.json content (schema + aliases)
    sqlite=[<size>B mtime=<ts>] stat of collection/document_child_chunks/storage.sqlite
    git=<sha>                   git log -1 --format=%h for qdrant_db/ (or "none"/"unavailable")

    Divergence warning: the git SHA only changes when qdrant_db/ is committed.
    If the production index was re-ingested but not committed, the SHA stays
    stale — run with --db <path> (server stopped) and compare the sqlite stat.
    """
    meta_file = db_path / "meta.json"
    meta_hash = hashlib.sha256(meta_file.read_bytes()).hexdigest()[:16]

    sqlite_file = db_path / "collection" / "document_child_chunks" / "storage.sqlite"
    if sqlite_file.exists():
        st = sqlite_file.stat()
        sqlite_stat = f"{st.st_size}B mtime={int(st.st_mtime)}"
    else:
        sqlite_stat = "NOT_FOUND"

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", db_path.name],
            capture_output=True,
            text=True,
            cwd=str(db_path.parent),
            timeout=5,
        )
        git_sha = result.stdout.strip() or "none"
    except Exception:
        git_sha = "unavailable"

    return f"meta={meta_hash} sqlite=[{sqlite_stat}] git={git_sha}"


def _copy_open_qdrant(db_path: Path):
    """Copy *db_path* to a temp dir (ignoring .lock) and return (QdrantClient, tmp_dir).

    The caller is responsible for closing the client and cleaning up tmp_dir.
    Verified pattern: 0.04 s copy, 0.75 s open, 1 459 points
    (WSL Python 3.14, qdrant-client 1.18.0, 2026-06-10).

    Uses only qdrant-client — torch-free, testable on dev/WSL.
    """
    from qdrant_client import QdrantClient  # qdrant-client installed in T1 venv

    tmp_dir = Path(tempfile.mkdtemp(prefix="repro_qdrant_"))
    shutil.copytree(
        str(db_path),
        str(tmp_dir / "db"),
        ignore=shutil.ignore_patterns(".lock", "*.lock"),
    )
    client = QdrantClient(path=str(tmp_dir / "db"))
    return client, tmp_dir


# ═════════════════════════════════════════════════════════════════════════════
#  Subcommand: rewrite
# ═════════════════════════════════════════════════════════════════════════════

def cmd_rewrite(args: argparse.Namespace) -> None:
    """Run the real rewrite_query chain (QueryAnalysis structured output) via Ollama."""
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if not ollama_url:
        print(
            "ERROR: OLLAMA_BASE_URL is not set in project/.env\n"
            "  repro rewrite requires a running Ollama server (e.g. qwen3.5:9b).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        print(f"ERROR: missing dep — {exc}", file=sys.stderr)
        sys.exit(1)

    from langchain_core.messages import HumanMessage, SystemMessage

    from rag_agent.nodes import _invoke_structured_rewrite
    from rag_agent.prompts import get_rewrite_query_prompt

    llm = ChatOllama(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE,
        reasoning=config.LLM_REASONING,
        num_ctx=config.LLM_NUM_CTX,
        base_url=ollama_url,
    )

    print("[repro rewrite]")
    print(f"  question : {args.question!r}")
    print(f"  ollama   : {ollama_url}  model={config.LLM_MODEL}")
    print()
    print("Running _invoke_structured_rewrite (QueryAnalysis structured output)…")

    context_section = f"사용자 질문:\n{args.question}\n"
    messages = [
        SystemMessage(content=get_rewrite_query_prompt()),
        HumanMessage(content=context_section),
    ]
    response = _invoke_structured_rewrite(llm, messages)

    print()
    print(f"  is_clear             : {response.is_clear}")
    if response.is_clear and response.questions:
        for i, q in enumerate(response.questions, 1):
            print(f"  rewritten_Q[{i}]       : {q}")
    if response.clarification_needed and response.clarification_needed.strip():
        print(f"  clarification_needed : {response.clarification_needed}")


# ═════════════════════════════════════════════════════════════════════════════
#  Subcommand: search
# ═════════════════════════════════════════════════════════════════════════════

def cmd_search(args: argparse.Namespace) -> None:
    """Re-execute the EXACT production search path with mandatory index fingerprint.

    Fidelity contract (Architect-mandated):
      1. Reproduces similarity_search(query, k, score_threshold) over HYBRID exactly.
      2. Index fingerprint printed on every run.
      3. Any similarity_search_with_score output labeled 'diagnostic hybrid-fusion
         score — NOT the production gate value'.
    """
    # ── dep check FIRST (names every missing dep specifically) ───────────────────
    missing: list[str] = []
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch")
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        missing.append("sentence-transformers")
    try:
        from qdrant_client import QdrantClient  # noqa: F401
    except ImportError:
        missing.append("qdrant-client")

    if missing:
        print(
            f"ERROR: missing production deps: {', '.join(missing)}\n"
            f"  `repro search` requires: torch, sentence-transformers, qdrant-client\n"
            f"  These are PRODUCTION-BOX-ONLY — not installable on WSL Python 3.14.\n"
            f"  On the production server: pip install torch sentence-transformers\n"
            f"  See docs/debugging/trace-to-root-cause.md §Repro fidelity caveats.",
            file=sys.stderr,
        )
        sys.exit(2)

    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

    db_path = Path(args.db) if args.db else Path(config.QDRANT_DB_PATH)
    threshold = args.threshold if args.threshold is not None else config.SEARCH_SCORE_THRESHOLD
    k = config.MAX_TOOL_CALLS  # matches production: rag_agent/tools.py:20 uses no explicit k override

    # ── index fingerprint (EVERY run — fidelity contract #2) ─────────────────────
    fingerprint = _qdrant_fingerprint(db_path)
    print(f"[repro search] index fingerprint: {fingerprint}")
    print(f"  query     : {args.query!r}")
    print(f"  threshold : {threshold}  (production default = {config.SEARCH_SCORE_THRESHOLD})")
    print(f"  k         : {k}  (= config.MAX_TOOL_CALLS, matches tools.py:20)")
    print(f"  db_path   : {db_path}")
    print()

    # ── open Qdrant: copy-to-temp unless --db supplied ────────────────────────────
    tmp_dir: Path | None = None
    if args.db:
        # --db: user stopped the server, open directly
        from qdrant_client import QdrantClient

        print("  [direct open — --db supplied; ensure the server is stopped]")
        qdrant_client = QdrantClient(path=str(db_path))
    else:
        # production holds the process-exclusive lock → copy first
        print("  Copying qdrant_db/ to temp dir (ignoring .lock)…", end=" ", flush=True)
        qdrant_client, tmp_dir = _copy_open_qdrant(db_path)
        print("done")

    try:
        # ── EXACT production embeddings (db/vector_db_manager.py:13-17) ──────────
        print(f"  Loading dense embeddings ({config.DENSE_MODEL}, device={config.EMBEDDING_DEVICE})…")
        dense_embeddings = HuggingFaceEmbeddings(
            model_name=config.DENSE_MODEL,
            model_kwargs={"device": config.EMBEDDING_DEVICE},
        )
        print(f"  Loading sparse embeddings ({config.SPARSE_MODEL})…")
        sparse_embeddings = FastEmbedSparse(model_name=config.SPARSE_MODEL)

        # ── EXACT production QdrantVectorStore (vector_db_manager.py:40-48) ──────
        collection = QdrantVectorStore(
            client=qdrant_client,
            collection_name=config.CHILD_COLLECTION,
            embedding=dense_embeddings,
            sparse_embedding=sparse_embeddings,
            retrieval_mode=RetrievalMode.HYBRID,
            sparse_vector_name=config.SPARSE_VECTOR_NAME,
        )

        # ── EXACT production call (rag_agent/tools.py:20) ────────────────────────
        print(f"\n  Running: collection.similarity_search(query, k={k}, score_threshold={threshold})\n")
        results = collection.similarity_search(args.query, k=k, score_threshold=threshold)

        if not results:
            print("  → NO_RELEVANT_CHUNKS  (production returns this sentinel → refusal path)")
        else:
            print(f"  → {len(results)} chunk(s) PASS (hybrid-fusion score ≥ {threshold}):\n")
            for i, doc in enumerate(results, 1):
                parent_id = doc.metadata.get("parent_id", "")
                source = doc.metadata.get("source", "")
                preview = doc.page_content[:120].replace("\n", " ").strip()
                print(f"  [{i}] PASS  parent_id={parent_id}  source={source}")
                print(f"       {preview!r}…")
                print()

        # ── diagnostic aux scores (fidelity contract #1 — label is MANDATORY) ────
        print("  ─── Diagnostic (for exploration only — NOT the production gate) ────────────")
        print("  similarity_search_with_score returns hybrid-fusion (RRF) rank scores.")
        print("  These are rank-based (top hit ≈ 0.5, next ≈ 0.33, 0.25 …) — NOT cosine")
        print("  similarity. The threshold filter runs INSIDE hybrid fusion; the two numbers")
        print("  are NOT directly comparable.")
        print("  Label: 'diagnostic hybrid-fusion score — NOT the production gate value'")
        print()

        with_scores = collection.similarity_search_with_score(args.query, k=k + 4)
        show = min(10, len(with_scores))
        for doc, score in with_scores[:show]:
            parent_id = doc.metadata.get("parent_id", "")
            gate = "PASS" if score >= threshold else "FAIL"
            print(
                f"    diag_hybrid_score={score:.4f}  [{gate} vs threshold={threshold}]"
                f"  parent_id={parent_id}"
            )
        if len(with_scores) > show:
            print(f"    … ({len(with_scores) - show} more not shown)")

    finally:
        qdrant_client.close()
        if tmp_dir is not None and tmp_dir.exists():
            shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Subcommand: chunk
# ═════════════════════════════════════════════════════════════════════════════

def cmd_chunk(args: argparse.Namespace) -> None:
    """Run DocumentChuncker on a markdown file; print parent/child boundaries + sizes."""
    from document_chunker import DocumentChuncker

    md_path = Path(args.md_file)
    if not md_path.exists():
        # also try relative to markdown_docs/
        alt = _WORKTREE / "markdown_docs" / args.md_file
        if alt.exists():
            md_path = alt
        else:
            print(f"ERROR: file not found: {args.md_file!r}", file=sys.stderr)
            print(f"  (also tried: {alt})", file=sys.stderr)
            sys.exit(1)

    chunker = DocumentChuncker()
    parents, children = chunker.create_chunks_single(md_path)

    print(f"[repro chunk] {md_path.name}")
    print(f"  parents  : {len(parents)}")
    print(f"  children : {len(children)}")
    print()

    for i, (parent_id, p_doc) in enumerate(parents):
        p_size = len(p_doc.page_content)
        children_of = [c for c in children if c.metadata.get("parent_id") == parent_id]
        header_meta = {k: v for k, v in p_doc.metadata.items() if k not in ("parent_id", "source")}
        print(f"  parent[{i}]  id={parent_id}  size={p_size}ch  children={len(children_of)}")
        if header_meta:
            print(f"             headers={header_meta}")
        preview = p_doc.page_content[:100].replace("\n", "↵ ").strip()
        print(f"             preview: {preview!r}…")
        for j, c in enumerate(children_of):
            c_size = len(c.page_content)
            c_prev = c.page_content[:60].replace("\n", "↵ ").strip()
            print(f"    child[{j}]   size={c_size}ch  {c_prev!r}…")
        print()


# ═════════════════════════════════════════════════════════════════════════════
#  Subcommand: parent
# ═════════════════════════════════════════════════════════════════════════════

def cmd_parent(args: argparse.Namespace) -> None:
    """Fetch a parent chunk from parent_store/ and print its content."""
    from db.parent_store_manager import ParentStoreManager

    store = ParentStoreManager()
    try:
        result = store.load_content(args.parent_id)
    except FileNotFoundError:
        print(f"ERROR: parent_id not found: {args.parent_id!r}", file=sys.stderr)
        print(f"  parent_store path: {config.PARENT_STORE_PATH}", file=sys.stderr)
        available = sorted(
            Path(config.PARENT_STORE_PATH).glob("*.json"),
            key=lambda p: p.stem,
        )[:5]
        if available:
            print(f"  Available (first 5): {[p.stem for p in available]}", file=sys.stderr)
        sys.exit(1)

    print(f"[repro parent] {args.parent_id}")
    print(f"  source   : {result.get('metadata', {}).get('source', 'unknown')}")
    print(f"  size     : {len(result.get('content', ''))} chars")
    print()
    print(result.get("content", ""))


# ═════════════════════════════════════════════════════════════════════════════
#  Subcommand: answer
# ═════════════════════════════════════════════════════════════════════════════

def cmd_answer(args: argparse.Namespace) -> None:
    """Single-shot e2e answer via the full RAG graph (Ollama + Qdrant).

    Non-deterministic: a runaway answer like the 290 s / 21.5k-char case in
    trace a687e093 (Langfuse 51c47a5061f70aa2) may not reproduce on demand.
    See docs/debugging/trace-to-root-cause.md §Repro fidelity caveats.
    PRODUCTION-BOX-ONLY: requires torch + sentence-transformers + OLLAMA_BASE_URL.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if not ollama_url:
        print(
            "ERROR: OLLAMA_BASE_URL is not set in project/.env\n"
            "  repro answer requires a running Ollama server.",
            file=sys.stderr,
        )
        sys.exit(1)

    missing: list[str] = []
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch")
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        missing.append("sentence-transformers")
    if missing:
        print(
            f"ERROR: missing production deps: {', '.join(missing)}\n"
            f"  `repro answer` requires torch + sentence-transformers"
            f" (PRODUCTION-BOX-ONLY).",
            file=sys.stderr,
        )
        sys.exit(2)

    from langchain_core.messages import AIMessage, HumanMessage

    from core.rag_system import RAGSystem

    print("[repro answer]")
    print(f"  question : {args.question!r}")
    print(f"  ollama   : {ollama_url}  model={config.LLM_MODEL}")
    print()
    print(
        "  NOTE: answer is non-deterministic. The 290 s / 21.5k-char runaway (tid=a687e093,\n"
        "  Langfuse 51c47a5061f70aa2) may not reproduce on demand — expected variance per\n"
        "  docs/debugging/trace-to-root-cause.md §Repro fidelity caveats."
    )
    print()
    print("Initialising RAG system (loads embeddings + compiles graph)…")

    rs = RAGSystem()
    rs.initialize()
    cfg = rs.get_config()

    print("Invoking graph…\n")
    result = rs.agent_graph.invoke(
        {"messages": [HumanMessage(content=args.question)]},
        config=cfg,
    )

    messages = result.get("messages", [])
    last = messages[-1] if messages else None
    if last is None:
        print("(no messages in graph output)", file=sys.stderr)
        sys.exit(1)

    content = last.content if isinstance(last, AIMessage) else str(last)
    print(content)


# ═════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m debug.repro",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_ENV_MATRIX,
    )
    sub = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")
    sub.required = True

    # ── rewrite ──────────────────────────────────────────────────────────────
    p_rw = sub.add_parser(
        "rewrite",
        help="Run the real rewrite_query chain via Ollama (needs OLLAMA_BASE_URL)",
        description="Run the production rewrite_query path (QueryAnalysis structured output).",
    )
    p_rw.add_argument("question", help="User question to rewrite")

    # ── search ───────────────────────────────────────────────────────────────
    p_sr = sub.add_parser(
        "search",
        help="Re-execute EXACT production search — PROD-BOX-ONLY (needs torch+sentence-transformers)",
        description=(
            "Reproduces the exact production call: "
            "similarity_search(query, k, score_threshold) over RetrievalMode.HYBRID "
            "(dense bge-m3 + sparse bm25). Prints the index fingerprint on every run."
        ),
    )
    p_sr.add_argument("query", help="Search query (typically the rewritten question)")
    p_sr.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="X",
        help=(
            f"Score threshold override (default: config.SEARCH_SCORE_THRESHOLD"
            f"={config.SEARCH_SCORE_THRESHOLD}). "
            "Relevancy-check isolation lever: each chunk is labelled PASS/FAIL vs this gate."
        ),
    )
    p_sr.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Use an explicit Qdrant DB path (skip copy-to-temp). Requires server stopped.",
    )

    # ── chunk ─────────────────────────────────────────────────────────────────
    p_ck = sub.add_parser(
        "chunk",
        help="Run DocumentChuncker on a markdown file (app-imports only, works on WSL)",
    )
    p_ck.add_argument(
        "md_file",
        help="Path to a .md file (absolute, or filename in markdown_docs/)",
    )

    # ── parent ────────────────────────────────────────────────────────────────
    p_pr = sub.add_parser(
        "parent",
        help="Fetch a parent chunk from parent_store/ (app-imports only, works on WSL)",
    )
    p_pr.add_argument(
        "parent_id",
        help="Parent chunk ID, e.g. '2026학년도1학기학사안내_parent_0'",
    )

    # ── answer ────────────────────────────────────────────────────────────────
    p_an = sub.add_parser(
        "answer",
        help="Single-shot e2e RAG answer — PROD-BOX-ONLY, non-deterministic",
        description=(
            "Run the full RAG graph end-to-end. "
            "Non-deterministic: the 290 s runaway (trace a687e093) may not reproduce."
        ),
    )
    p_an.add_argument("question", help="User question")

    return parser


def main() -> None:
    _bootstrap_env()
    parser = _build_parser()
    args = parser.parse_args()
    dispatch = {
        "rewrite": cmd_rewrite,
        "search": cmd_search,
        "chunk": cmd_chunk,
        "parent": cmd_parent,
        "answer": cmd_answer,
    }
    dispatch[args.subcmd](args)


if __name__ == "__main__":
    main()
