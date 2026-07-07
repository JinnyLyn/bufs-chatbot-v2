"""4-bucket retrieval rank probe (issue #104 method) — separates agent-side query loss
(term-drift) from retriever-side limits, WITHOUT the agent in the loop.

For every dataset question the ORIGINAL text is sent straight to the embedded-Qdrant
HYBRID index (top ``--k``, no score threshold), then bucketed:

  gold doc in top-k?                   No  -> embedding      (index can't surface the doc)
  answer chunk (any-token) in top-5?   Yes -> captured@5     (retriever fine on original text)
  answer chunk in 6..k?                    -> rank_cut       (bigger k / limit recovers it)
  else                                     -> chunking       (doc arrives, answer chunk never does)

``captured@5(sub-thr)`` flags hits that rank <=5 but score below SEARCH_SCORE_THRESHOLD —
they pass the probe yet get filtered live. Chunk-hit criterion is **any-token** (one of
``must_include`` inside the chunk text, whitespace-normalized) for #104 comparability, so
rank/chunking problems are slightly UNDER-counted (loose criterion).

Cross-tab (``--run``): joins a ``_ragas_kpi`` result json; live failures that the probe
captures@5 are **agent-side losses (term-drift, #87)** — the index serves the answer for
the original text, so the agent's reformulated query lost it. ``--legs`` then attributes
those to the dense vs sparse leg (2026-07-07 실측: 17/19 dense-소관 → sparse-만 원문을
주입하는 split-path 로는 회복 불가).

⚠ STOP the backend first — the embedded Qdrant is a single-process lock. 100 questions
run in ~0.2 min (query embedding only).

Run::

  python eval_tools/_rank_probe.py                                    # probe only
  python eval_tools/_rank_probe.py --run logs/ragas_kpi_full9b_latest.json \
      --buckets logs/error_buckets_20260707_145259.json --legs        # full cross-tab

Outputs: logs/rank_probe_<ts>.json and logs/rank_probe_latest.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_scorer  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETRIEVAL_BUCKETS = ("Chunk 문제", "Embedding 문제", "문서 없음")


def _norm(s: str) -> str:
    return re.sub(r"[\s\-\(\)\[\]\.·/,_]+", "", (s or "").lower())


def _doc_match(gold: str, source: str) -> bool:
    # Strip the extension BEFORE normalizing — _norm() removes dots, so the pattern
    # would never match afterwards and a stray "pdf" suffix breaks s-in-g containment.
    base = re.sub(r"\.(pdf|md|markdown|txt|docx?)$", "", os.path.basename(source or ""), flags=re.I)
    g, s = _norm(gold), _norm(base)
    return bool(g) and (g in s or s in g)


def _best_rank(hits, toks):
    """(rank, score) of the first hit whose text contains ANY must_include token."""
    for j, (d, s) in enumerate(hits, 1):
        if toks and any(t in _norm(d.page_content) for t in toks):
            return j, s
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(_REPO, "eval_tools", "datasets", "qa_dataset.json"))
    ap.add_argument("--run", default=None, help="ragas_kpi result json to cross-tab live outcomes")
    ap.add_argument("--buckets", default=None, help="error_buckets json (per-id 7-bucket labels)")
    ap.add_argument("--k", type=int, default=20, help="probe depth (top-k)")
    ap.add_argument("--legs", action="store_true",
                    help="dense/sparse leg attribution for live-failed probe-captured ids (term-drift)")
    args = ap.parse_args()

    # Heavy imports here so the module stays importable without the project deps.
    sys.path.insert(0, os.path.join(_REPO, "project"))
    import config  # noqa: E402
    from db.vector_db_manager import VectorDbManager  # noqa: E402

    thresh = config.SEARCH_SCORE_THRESHOLD
    data = qa_scorer.load_dataset(args.dataset)
    run_by_id, bucket_by_id = {}, {}
    if args.run:
        run_by_id = {r["id"]: r for r in json.load(open(args.run, encoding="utf-8"))["results"]}
    if args.buckets:
        bucket_by_id = {r["id"]: r.get("bucket") for r in json.load(open(args.buckets, encoding="utf-8"))["rows"]}

    print(f"probe | k={args.k} live-threshold={thresh} split_path(live)={config.SPLIT_PATH_ENABLED} "
          f"| dense={config.DENSE_MODEL} sparse={config.SPARSE_MODEL} idf={config.SPARSE_IDF}", flush=True)
    vdm = VectorDbManager()
    vs = vdm.get_collection(config.CHILD_COLLECTION)

    out, t0 = [], time.time()
    for i, rec in enumerate(data, 1):
        q, gold = rec["question"], rec["gold_document"]
        toks = [_norm(t) for t in rec.get("must_include", []) if t]
        hits = vs.similarity_search_with_score(q, k=args.k)  # threshold 0: see everything
        doc_ranks = [j for j, (d, _) in enumerate(hits, 1) if _doc_match(gold, d.metadata.get("source", ""))]
        rank, score = _best_rank(hits, toks)

        if not qa_scorer.is_retrievable_gold(gold):
            bucket = "n/a(기타)"
        elif not doc_ranks:
            bucket = "embedding"
        elif rank and rank <= 5:
            bucket = "captured@5" + ("(sub-thr)" if score < thresh else "")
        elif rank:
            bucket = f"rank_cut(6-{args.k})"
        else:
            bucket = "chunking"

        rr = run_by_id.get(rec["id"], {})
        out.append({"id": rec["id"], "category": rec.get("category"), "question": q,
                    "gold_document": gold, "probe_bucket": bucket,
                    "ans_rank": rank, "ans_score": round(score, 4) if score is not None else None,
                    "doc_best_rank": doc_ranks[0] if doc_ranks else None,
                    "live_corr": rr.get("answer_correctness"), "live_cr": rr.get("context_recall"),
                    "live_doc_hit": rr.get("doc_hit"), "live_bucket": bucket_by_id.get(rec["id"])})
        if i % 20 == 0:
            print(f"  [{i:3}/{len(data)}] {(time.time() - t0) / 60:.1f}m", flush=True)

    # ---- summary ----
    print(f"\n=== probe buckets (원문 질의 hybrid top-{args.k}, n={len(out)}) ===")
    for b, c in Counter(r["probe_bucket"] for r in out).most_common():
        print(f"  {b:<20} {c}")
    sub = [r["id"] for r in out if r["probe_bucket"] == "captured@5(sub-thr)"]
    if sub:
        print(f"  ⚠ threshold({thresh})로 라이브에서 잘리는 top-5 히트: {len(sub)}건 ids={sub}")

    drift = []
    if run_by_id:
        wrong = [r for r in out if isinstance(r["live_corr"], (int, float)) and 0 <= r["live_corr"] < 0.5]
        # retrieval-side: bucket labels when provided, else low-context_recall proxy
        if bucket_by_id:
            ret = [r for r in wrong if (r["live_bucket"] or "") in RETRIEVAL_BUCKETS]
            how = "7-bucket 라벨"
        else:
            ret = [r for r in wrong if isinstance(r["live_cr"], (int, float)) and 0 <= r["live_cr"] < 0.5]
            how = "context_recall<0.5 근사"
        print(f"\n=== 교차: 라이브 오답 {len(wrong)} / 검색-side {len(ret)} ({how}) ===")
        for b, c in Counter(r["probe_bucket"] for r in ret).most_common():
            print(f"  probe={b:<20} {c}")
        drift = [r for r in ret if r["probe_bucket"].startswith("captured@5")]
        print(f"\n★ agent-side loss(term-drift 등: 원문이면 top-5인데 라이브 실패): {len(drift)}건")
        for r in drift:
            print(f"   id={r['id']:>3} rank={r['ans_rank']} score={r['ans_score']} | {r['question'][:44]}")

    if args.legs and drift:
        from langchain_qdrant import QdrantVectorStore, RetrievalMode  # noqa: E402
        dense = QdrantVectorStore(client=vs.client, collection_name=config.CHILD_COLLECTION,
                                  embedding=vs.embeddings, retrieval_mode=RetrievalMode.DENSE)
        sparse = QdrantVectorStore(client=vs.client, collection_name=config.CHILD_COLLECTION,
                                   sparse_embedding=vs.sparse_embeddings, retrieval_mode=RetrievalMode.SPARSE,
                                   sparse_vector_name=config.SPARSE_VECTOR_NAME)
        by_id = {r["id"]: r for r in data}
        cnt = Counter()
        print(f"\n=== 레그 귀속 (drift {len(drift)}건, 원문 질의 top-5 기준) ===")
        print(f"{'id':>4} {'dense':>6} {'sparse':>7}")
        for r in drift:
            rec = by_id[r["id"]]
            toks = [_norm(t) for t in rec.get("must_include", []) if t]
            rd, _ = _best_rank(dense.similarity_search_with_score(rec["question"], k=args.k), toks)
            rs, _ = _best_rank(sparse.similarity_search_with_score(rec["question"], k=args.k), toks)
            r["dense_rank"], r["sparse_rank"] = rd, rs
            d5, s5 = (rd or 99) <= 5, (rs or 99) <= 5
            cnt["dense만" if d5 and not s5 else "sparse만" if s5 and not d5
                else "양쪽" if d5 else "융합효과"] += 1
            print(f"{r['id']:>4} {str(rd):>6} {str(rs):>7}")
        print(f"  귀속: {dict(cnt)}  (dense-소관이 다수면 sparse-만 원문 주입하는 split-path로는 회복 불가)")

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(_REPO, "logs"), exist_ok=True)
    for name in (f"rank_probe_{ts}.json", "rank_probe_latest.json"):
        with open(os.path.join(_REPO, "logs", name), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\nreport -> logs/rank_probe_{ts}.json  (latest -> logs/rank_probe_latest.json)")


if __name__ == "__main__":
    main()
