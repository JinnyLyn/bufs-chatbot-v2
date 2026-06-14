"""Standalone retrieval probe — isolate WHY the 학사일정 calendar chunk doesn't surface for
date queries. Opens the embedded Qdrant directly (STOP the backend first — single-process
lock) and runs the SAME bge-m3 + bm25 vectors in three modes (DENSE / SPARSE / HYBRID) for
spaced vs joined query forms, reporting where the finals-calendar chunk ranks and its score.
Run from MAIN repo:  python ..\\<worktree>\\eval_tools\\_retrieval_probe.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "project"))
from dotenv import load_dotenv
load_dotenv("project/.env")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from langchain_qdrant import QdrantVectorStore, FastEmbedSparse, RetrievalMode
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient

client = QdrantClient(path=config.QDRANT_DB_PATH)
dense = HuggingFaceEmbeddings(model_name=config.DENSE_MODEL, model_kwargs={"device": config.EMBEDDING_DEVICE})
sparse = FastEmbedSparse(model_name=config.SPARSE_MODEL)

def store(mode, **kw):
    return QdrantVectorStore(client=client, collection_name=config.CHILD_COLLECTION,
                             embedding=dense, sparse_embedding=sparse,
                             retrieval_mode=mode, sparse_vector_name=config.SPARSE_VECTOR_NAME, **kw)

stores = {
    "DENSE":  store(RetrievalMode.DENSE),
    "SPARSE": store(RetrievalMode.SPARSE),
    "HYBRID": store(RetrievalMode.HYBRID),
}

QUERIES = [
    "2026학년도 1학기 기말고사 기간은 언제인가?",   # the real eval question (spaced)
    "기말고사 기간",                                # natural, spaced
    "기말고사기간 6월",                             # joined token (matches corpus cell)
]
def is_target(text):  # the chunk that actually holds the FINALS DATE (not incidental 기말고사 mentions)
    t = text.replace(" ", "")
    return ("기말고사기간:6월8일" in t) or ("|6|8(월)~12" in t) or ("기말고사기간6월8일" in t)

print(f"threshold={config.SEARCH_SCORE_THRESHOLD}  dense={config.DENSE_MODEL}  sparse={config.SPARSE_MODEL}\n" + "=" * 80)
for q in QUERIES:
    print(f"\nQUERY: {q!r}")
    for name, st in stores.items():
        try:
            hits = st.similarity_search_with_score(q, k=20)
        except Exception as e:
            print(f"  {name:7} ERROR {e}"); continue
        rank = next((i for i, (d, s) in enumerate(hits, 1) if is_target(d.page_content)), None)
        tgt = next(((d, s) for d, s in hits if is_target(d.page_content)), None)
        top = hits[0] if hits else None
        topscore = f"{top[1]:.4f}" if top else "-"
        if tgt:
            passes = "PASS" if tgt[1] >= config.SEARCH_SCORE_THRESHOLD else "FILTERED(<thr)"
            print(f"  {name:7} 기말고사청크 rank={rank}/20 score={tgt[1]:.4f} [{passes}]  | top1 score={topscore}")
        else:
            print(f"  {name:7} 기말고사청크 NOT in top-20            | top1 score={topscore}")
    # show hybrid top-5 to see what outranks it
    print("   HYBRID top-5:")
    for d, s in stores["HYBRID"].similarity_search_with_score(q, k=5):
        mark = " <== 기말고사" if is_target(d.page_content) else ""
        print(f"     {s:.4f}  {d.page_content[:54].replace(chr(10),' ')}{mark}")
print("\ndone.")
