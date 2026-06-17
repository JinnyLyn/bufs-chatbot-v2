"""Isolated retrieval-quality A/B across sparse-tokenizer variants — NO LLM needed.

Opens the embedded Qdrant directly (stop any backend on this DB first) and, for each
variant collection, measures how well the lexical (SPARSE) and full (HYBRID) retrievers
surface answer-bearing chunks for the combined88 questions. Relevance is a proxy: a
retrieved child chunk is "gold" if it contains the ground-truth facts auto-extracted
from the question (numbers/dates/grades), reusing the combined88 scorer's logic.

Metrics per variant, per mode (SPARSE / HYBRID), over answerable questions that have
extractable facts:
  - recall@k (strict): fraction with a chunk containing ALL gold facts in top-k
  - MRR (strict): mean reciprocal rank of the first all-facts chunk
  - coverage@k: mean over questions of the best single-chunk fact coverage in top-k

Run from MAIN repo or worktree (Okt needs JAVA_HOME):
    JAVA_HOME=... python eval_tools/_retrieval_recall.py [--k 10]
"""
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "project")
sys.path.insert(0, _PROJECT)
from dotenv import load_dotenv

load_dotenv(os.path.join(_PROJECT, ".env"))

import config
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient

SRC = r"C:\Users\suhwa\Desktop\bufs-chatbot\reports\retrieval_eval\combined88_results_fix_20260429.json"
OUT = os.path.join(_PROJECT, "..", "logs", "retrieval_recall.json")

K = 10
if "--k" in sys.argv:
    K = int(sys.argv[sys.argv.index("--k") + 1])

# (label, collection, sparse_kind, sparse_arg). sparse_kind: "fastembed" | "korean" | "bm42_kiwi".
VARIANTS = [
    ("V0_bm25_noidf", "document_child_chunks", "fastembed", "Qdrant/bm25"),
    ("V1_bm25_idf", "document_child_chunks__bm25_idf", "fastembed", "Qdrant/bm25"),
    ("V2_kiwi_idf", "document_child_chunks__kiwi_idf", "korean", "kiwi"),
    ("V3_okt_idf", "document_child_chunks__okt_idf", "korean", "okt"),
    ("V4_bm42_idf", "document_child_chunks__bm42_idf", "fastembed", "Qdrant/bm42-all-minilm-l6-v2-attentions"),
    ("V5_bm42_kiwi_idf", "document_child_chunks__bm42_kiwi_idf", "bm42_kiwi", None),
]


# --- combined88 fact extraction (copied from eval_tools/_eval_combined88.py) ---
def extract_facts(gt: str):
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s):
            facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"[A-F]\+", s):
        facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n):
            continue
        facts.add(n)
    return facts


def matched(fact: str, answer: str) -> bool:
    a = answer
    if re.fullmatch(r"\d+", fact):
        return re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")) is not None
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        return fact in a.replace(" ", "")
    return fact in a


def build_sparse(kind, arg):
    if kind == "korean":
        from db.korean_sparse import KoreanBM25Sparse
        return KoreanBM25Sparse(arg)
    if kind == "bm42_kiwi":
        from db.korean_sparse import Bm42KiwiSparse
        return Bm42KiwiSparse()
    return FastEmbedSparse(model_name=arg)


def main():
    data = json.load(open(SRC, encoding="utf-8"))["results"]
    qs = [r for r in data if r.get("answerable", True)]
    # keep only questions with extractable numeric/date/grade facts (clean gold signal)
    items = []
    for r in qs:
        facts = extract_facts(r.get("ground_truth", ""))
        if facts:
            items.append((r["question"], facts))
    print(f"combined88: {len(qs)} answerable, {len(items)} with extractable facts. k={K}")

    client = QdrantClient(path=config.QDRANT_DB_PATH)
    existing = {c.name for c in client.get_collections().collections}
    dense = HuggingFaceEmbeddings(model_name=config.DENSE_MODEL, model_kwargs={"device": config.EMBEDDING_DEVICE})

    summary = {}
    for label, coll, kind, arg in VARIANTS:
        if coll not in existing:
            print(f"\n[{label}] collection {coll!r} missing — skip (build it first)")
            continue
        sparse = build_sparse(kind, arg)
        stores = {
            "SPARSE": QdrantVectorStore(client=client, collection_name=coll, embedding=dense,
                                        sparse_embedding=sparse, retrieval_mode=RetrievalMode.SPARSE,
                                        sparse_vector_name=config.SPARSE_VECTOR_NAME),
            "HYBRID": QdrantVectorStore(client=client, collection_name=coll, embedding=dense,
                                        sparse_embedding=sparse, retrieval_mode=RetrievalMode.HYBRID,
                                        sparse_vector_name=config.SPARSE_VECTOR_NAME),
        }
        res = {}
        for mode, st in stores.items():
            recall_hits = rr_sum = cov_sum = 0
            for q, facts in items:
                docs = [d for d, _ in st.similarity_search_with_score(q, k=K)]
                best_cov, first_all = 0.0, None
                for rank, d in enumerate(docs, 1):
                    hit = sum(1 for f in facts if matched(f, d.page_content))
                    cov = hit / len(facts)
                    best_cov = max(best_cov, cov)
                    if hit == len(facts) and first_all is None:
                        first_all = rank
                cov_sum += best_cov
                if first_all is not None:
                    recall_hits += 1
                    rr_sum += 1.0 / first_all
            n = len(items)
            res[mode] = {
                f"recall@{K}": round(recall_hits / n, 4),
                "mrr": round(rr_sum / n, 4),
                f"coverage@{K}": round(cov_sum / n, 4),
            }
        summary[label] = {"collection": coll, **res}
        s, h = res["SPARSE"], res["HYBRID"]
        print(f"\n[{label}]  ({coll})")
        print(f"   SPARSE  recall@{K}={s[f'recall@{K}']:.3f}  mrr={s['mrr']:.3f}  cov@{K}={s[f'coverage@{K}']:.3f}")
        print(f"   HYBRID  recall@{K}={h[f'recall@{K}']:.3f}  mrr={h['mrr']:.3f}  cov@{K}={h[f'coverage@{K}']:.3f}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"k": K, "n_items": len(items), "variants": summary}, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\nreport -> {os.path.normpath(OUT)}")
    client.close()


if __name__ == "__main__":
    main()
