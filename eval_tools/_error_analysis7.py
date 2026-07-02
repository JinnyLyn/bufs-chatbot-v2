"""오답 7버킷 분류 드라이버 — 순수 분류기(:mod:`error_taxonomy`)에 신호를 먹여 오답을
검색실패/Prompt실패/문서없음/질문애매/Hallucination/Chunk/Embedding 으로 귀인·집계한다.

기존 :mod:`_answer_analysis` (검색-vs-생성 2버킷)의 후속. 순수 규칙은 ``error_taxonomy`` 에
두고, 여기서는 **신호를 계산**한다(휴리스틱을 한곳에 모아 가시화):

  · is_correct        must_include 골드 사실이 모두 답변에 있나
  · evidence_retrieved 답에 빠진 사실이 검색된 컨텍스트엔 있었나 (컨텍스트 확보 시)
  · in_kb / chunk_split KBCorpus(오프라인) 로 코퍼스 존재·청크경계 조회
  · hallucinated      금지어(must_not_include) 누출 또는 RAGAS faithfulness < 임계
  · ranked_out        (라이브 인덱스 필요) 현재 드라이버는 미측정 → ⑦은 --langfuse+인덱스 확장분

입력 3모드 (전부 데이터셋 = in-repo ``qa_dataset.json``, id/질문으로 조인):
  --from-dump PATH  저장된 예측 덤프(answer[, context_preview][, faithfulness])로 **오프라인** 분류.
                    (RAGAS 덤프 ``logs/ragas_new_ollama_*.json`` 가 이 스키마) — 인프라 0으로 재현.
  --langfuse        라이브 트레이스에서 답변+컨텍스트를 끌어와 분류(_answer_analysis 와 동일 경로).
  --dry-run         합성 시나리오로 7버킷 자기점검 + 실제 KBCorpus 통계 (완전 오프라인 sanity).

결과: 콘솔 분포/사이드 롤업/카테고리 분해/버킷별 예시 + ``logs/error_taxonomy_result.json``.
컨텍스트/순위 신호가 없으면 순수 분류기가 정직하게 상위(미분리) 버킷으로 저하한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # import qa_scorer / error_taxonomy
import error_taxonomy as et
import qa_scorer
from error_taxonomy import KBCorpus, Signals, classify, present
from kpi.scorer import extract_facts  # 코퍼스-보정 사실 추출(날짜/숫자/등급) — generic-dump 폴백용

FAITH_THRESHOLD = 0.5  # RAGAS faithfulness 이 값 미만 + 근거검색됨 → Hallucination 신호


# --------------------------------------------------------------------------- 신호 계산

def build_signals(rec: dict, answer: str, contexts: str | None, corpus: KBCorpus,
                  faithfulness: float | None = None) -> tuple[Signals, dict]:
    """한 데이터셋 레코드 + 관측(answer/contexts/faithfulness) → :class:`Signals` + 진단.

    ``contexts`` 가 ``None`` 이면 컨텍스트 신호 없음(라이브 로그 미확보/덤프 미포함)으로
    처리 → 분류기가 KB-only 귀인 또는 미분리 버킷으로 저하한다.
    """
    facts = set(rec.get("must_include") or [])
    if not facts:
        return Signals(is_correct=False, has_facts=False), {"facts": []}

    in_ans = {f for f in facts if present(f, answer)}
    missing = facts - in_ans
    is_correct = not missing

    diag: dict = {"facts": sorted(facts), "in_answer": sorted(in_ans), "missing": sorted(missing)}

    if contexts is not None:
        in_ctx = {f for f in facts if present(f, contexts)}
        retr_facts = missing - in_ctx                      # 답·컨텍스트 둘 다 없는 사실
        evidence_retrieved = (len(retr_facts) == 0) if missing else None
        diag["in_context"] = sorted(missing & in_ctx)
    else:
        evidence_retrieved = None
        retr_facts = missing                                # 컨텍스트 모름 → 전 missing에 KB 귀인

    in_kb = chunk_split = None
    if retr_facts and evidence_retrieved is not True:
        not_in_kb = [f for f in retr_facts if not corpus.fact_in_kb(f)]
        in_kb = len(not_in_kb) == 0
        diag["not_in_kb"] = sorted(not_in_kb)
        if in_kb:
            split = [f for f in retr_facts if corpus.fact_split(f)]
            chunk_split = bool(split)
            diag["chunk_split_facts"] = sorted(split)

    # 생성-side: 금지어 누출(오프라인) 또는 faithfulness 저하 → Hallucination 신호.
    leaked = [t for t in (rec.get("must_not_include") or []) if qa_scorer.contains(t, answer)]
    hallucinated: bool | None = None
    if leaked:
        hallucinated = True
        diag["leaked_forbidden"] = leaked
    elif faithfulness is not None:
        hallucinated = faithfulness < FAITH_THRESHOLD
        diag["faithfulness"] = faithfulness

    sig = Signals(
        is_correct=is_correct,
        evidence_retrieved=evidence_retrieved,
        in_kb=in_kb,
        chunk_split=chunk_split,
        ranked_out=None,               # 라이브 인덱스 필요 — 현재 미측정
        hallucinated=hallucinated,
    )
    return sig, diag


# --------------------------------------------------------------------------- 리포트

def report(rows: list[dict], source: str, out_path: str) -> None:
    """분류 결과 rows(=[{id,category,bucket,reason,diag,...}]) → 콘솔 + JSON."""
    dist = Counter(r["bucket"] for r in rows)
    side = Counter(et.BUCKETS[r["bucket"]][1] for r in rows)
    n = len(rows)
    errs = n - dist["CORRECT"] - dist["NO_FACTS"]

    print(f"\n{'='*72}\n오답 7버킷 분류  (source={source}, matched {n} Qs, 오답 {errs})")
    for key in et.ORDER:
        c = dist.get(key, 0)
        if c:
            print(f"  {et.BUCKETS[key][0]:22} {c:3}  ({c/n*100:4.0f}%)")

    print(f"\n{'-'*72}\nside 롤업:")
    for s in ("correct", "retrieval", "generation", "question", "degraded", "skip"):
        if side.get(s):
            print(f"  {s:12} {side[s]:3}")

    by_cat: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_cat[r.get("category", "?")][r["bucket"]] += 1
    print(f"\n{'-'*72}\n카테고리 × 주요버킷 (정답/검색실패/Prompt/문서없음/Chunk/Hallu):")
    for cat, cc in sorted(by_cat.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(cc.values())
        print(f"  {cat:10} n={tot:2}  ok={cc['CORRECT']:2} 검색={cc['SEARCH_FAILURE']:2} "
              f"Prompt={cc['PROMPT_FAILURE']:2} 문서없음={cc['NO_DOCUMENT']:2} "
              f"Chunk={cc['CHUNK_PROBLEM']:2} Hallu={cc['HALLUCINATION']:2} "
              f"미분리={cc['GENERATION_UNSPLIT']+cc['RETRIEVAL_UNSPLIT']+cc['UNCLASSIFIED']:2}")

    print(f"\n{'-'*72}\n버킷별 예시(최대 4):")
    for key in et.ORDER:
        if key in ("CORRECT", "NO_FACTS"):
            continue
        ex = [r for r in rows if r["bucket"] == key][:4]
        if not ex:
            continue
        print(f"\n  ▸ {et.BUCKETS[key][0]}")
        for r in ex:
            print(f"    id={r['id']:<3} [{r.get('category','?')}] {r['reason']}")
            miss = r["diag"].get("missing")
            if miss:
                print(f"        missing={miss}")

    payload = {
        "source": source,
        "n": n,
        "distribution": {k: dist[k] for k in et.ORDER if dist.get(k)},
        "by_side": dict(side),
        "by_category": {c: dict(cc) for c, cc in by_cat.items()},
        "rows": rows,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n→ {out_path}")


# --------------------------------------------------------------------------- 입력 모드

def _index_dataset() -> tuple[dict, dict]:
    """qa_dataset 을 id / 정규화 질문 두 키로 인덱싱."""
    data = qa_scorer.load_dataset()
    by_id = {r["id"]: r for r in data}
    by_q = {qa_scorer._norm(r["question"]): r for r in data}
    return by_id, by_q


def run_from_dump(path: str, n: int | None) -> tuple[list[dict], str]:
    """저장된 예측 덤프(answer[, context_preview][, faithfulness])로 오프라인 분류."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    recs = d.get("results") if isinstance(d, dict) else d
    recs = recs if isinstance(recs, list) else []
    by_id, by_q = _index_dataset()
    corpus = KBCorpus.from_repo()
    rows: list[dict] = []
    synth = 0
    for obs in recs:
        gold = by_id.get(obs.get("id")) or by_q.get(qa_scorer._norm(obs.get("question", "")))
        if not gold:
            # generic-dump 폴백: 데이터셋에 없는 질문이면 reference/ground_truth 에서 사실을 뽑아
            # 임시 골드를 만든다(must_not_include 없음). qa_dataset-키 덤프가 아니어도 분류 가능.
            ref = str(obs.get("reference") or obs.get("ground_truth") or "")
            facts = extract_facts(ref)
            if not facts:
                continue
            gold = {"id": obs.get("id"), "category": obs.get("intent") or "generic",
                    "must_include": sorted(facts), "must_not_include": []}
            synth += 1
        answer = str(obs.get("answer") or "")
        ctx = obs.get("context_preview")
        if ctx is None:
            ctx = obs.get("contexts") or obs.get("context")
        if isinstance(ctx, list):
            ctx = "\n".join(str(c) for c in ctx)
        faith = obs.get("faithfulness")
        sig, diag = build_signals(gold, answer, ctx if ctx else None, corpus,
                                  faithfulness=faith if isinstance(faith, (int, float)) else None)
        v = classify(sig)
        rows.append({"id": gold["id"], "category": gold.get("category"),
                     "bucket": v.bucket, "reason": v.reason, "diag": diag})
        if n and len(rows) >= n:
            break
    has_ctx = bool(recs) and any(k in recs[0] for k in ("context_preview", "contexts", "context"))
    if synth:
        print(f"[note] {synth}/{len(rows)} 문항이 데이터셋에 없어 reference에서 사실을 합성함 "
              f"(must_not_include 가드 없음 → Hallucination은 faithfulness로만 판정)")
    if not rows:
        print(f"[warn] {os.path.basename(path)} 에서 분류 가능한 문항 0개 — id/질문이 qa_dataset과 "
              f"안 맞거나 reference/answer가 비어 있음.")
    return rows, f"dump:{os.path.basename(path)} (contexts={'yes' if has_ctx else 'n/a'}, synth={synth})"


def run_langfuse(n: int | None) -> tuple[list[dict], str]:
    """라이브 Langfuse에서 답변+검색 컨텍스트를 끌어와 분류 (_answer_analysis 와 동일 경로)."""
    import requests
    from dotenv import load_dotenv
    load_dotenv("project/.env")
    base = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    ca = os.environ.get("REQUESTS_CA_BUNDLE")

    def fetch(path, want, **params):
        out, page = [], 1
        while len(out) < want:
            data = None
            for a in range(6):
                try:
                    r = requests.get(base + path, params={"limit": 50, "page": page, **params},
                                     auth=auth, timeout=60, verify=ca)
                    if r.status_code in (429, 500, 502, 503, 504):
                        time.sleep(2 * (a + 1)); continue
                    r.raise_for_status(); data = r.json()["data"]; break
                except requests.exceptions.RequestException:
                    time.sleep(2 * (a + 1))
            if not data:
                break
            out.extend(data); page += 1
        return out[:want]

    def text(v):
        if isinstance(v, dict):
            msgs = v.get("messages") or []
            if msgs and isinstance(msgs[-1], dict):
                return str(msgs[-1].get("content", ""))
            return str(v.get("output") or v)
        return str(v or "")

    print("pulling traces + tool observations from Langfuse...", flush=True)
    traces = fetch("/api/public/traces", 260)
    latest: dict[str, tuple] = {}
    for t in traces:
        q = text(t.get("input")).strip()
        if not q:
            continue
        ts = t.get("timestamp") or ""
        if q not in latest or ts > latest[q][0]:
            latest[q] = (ts, t["id"], text(t.get("output")))
    ctx_by_trace: dict[str, list] = defaultdict(list)
    for name in ("search_child_chunks", "retrieve_parent_chunks"):
        for o in fetch("/api/public/observations", 400, type="TOOL", name=name):
            out = o.get("output")
            ctx_by_trace[o["traceId"]].append(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))

    corpus = KBCorpus.from_repo()
    rows: list[dict] = []
    for gold in qa_scorer.load_dataset():
        q = gold["question"].strip()
        if q not in latest:
            continue
        _, tid, answer = latest[q]
        ctx = "\n".join(ctx_by_trace.get(tid, [])) or None
        sig, diag = build_signals(gold, answer, ctx, corpus)
        v = classify(sig)
        rows.append({"id": gold["id"], "category": gold.get("category"),
                     "bucket": v.bucket, "reason": v.reason, "diag": diag})
        if n and len(rows) >= n:
            break
    return rows, "langfuse"


def run_dry_run() -> tuple[list[dict], str]:
    """합성 시나리오로 7버킷 자기점검 + 실제 KBCorpus 통계 (완전 오프라인)."""
    corpus = KBCorpus.from_repo()
    print(f"KBCorpus: sources={corpus.n_sources} docs, chunks={corpus.n_chunks}")
    scenarios = [
        ("CORRECT",            Signals(is_correct=True)),
        ("SEARCH_FAILURE",     Signals(is_correct=False, evidence_retrieved=False, in_kb=True)),
        ("PROMPT_FAILURE",     Signals(is_correct=False, evidence_retrieved=True, hallucinated=False)),
        ("NO_DOCUMENT",        Signals(is_correct=False, evidence_retrieved=False, in_kb=False)),
        ("AMBIGUOUS_QUESTION", Signals(is_correct=False, ambiguous=True)),
        ("HALLUCINATION",      Signals(is_correct=False, evidence_retrieved=True, hallucinated=True)),
        ("CHUNK_PROBLEM",      Signals(is_correct=False, evidence_retrieved=False, in_kb=True, chunk_split=True)),
        ("EMBEDDING_PROBLEM",  Signals(is_correct=False, evidence_retrieved=False, in_kb=True, chunk_split=False, ranked_out=True)),
        ("GENERATION_UNSPLIT", Signals(is_correct=False, evidence_retrieved=True)),
        ("RETRIEVAL_UNSPLIT",  Signals(is_correct=False, evidence_retrieved=False)),
        ("UNCLASSIFIED",       Signals(is_correct=False)),
    ]
    ok = True
    rows: list[dict] = []
    for i, (expect, sig) in enumerate(scenarios, 1):
        v = classify(sig)
        mark = "OK " if v.bucket == expect else "XX "
        if v.bucket != expect:
            ok = False
        print(f"  {mark}{expect:20} -> {v.bucket:20} {v.reason}")
        rows.append({"id": i, "category": "dry-run", "bucket": v.bucket,
                     "reason": v.reason, "diag": {"missing": [], "expected": expect}})
    # 실제 코퍼스로 in_kb/split 몇 개 시연
    for fact in ("복학", "계절학기 취소", "수강신청"):
        print(f"  corpus.fact_in_kb({fact!r}) = {corpus.fact_in_kb(fact)} | "
              f"split = {corpus.fact_split(fact)}")
    print("\nself-check:", "ALL OK" if ok else "MISMATCH!")
    return rows, "dry-run"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="오답 7버킷 분류기")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-dump", metavar="PATH", help="저장된 예측 덤프로 오프라인 분류")
    src.add_argument("--langfuse", action="store_true", help="라이브 Langfuse에서 분류")
    src.add_argument("--dry-run", action="store_true", help="합성 자기점검(오프라인)")
    ap.add_argument("--n", type=int, default=None, help="최대 문항 수")
    ap.add_argument("--out", default="logs/error_taxonomy_result.json")
    args = ap.parse_args()

    if args.from_dump:
        rows, source = run_from_dump(args.from_dump, args.n)
    elif args.langfuse:
        rows, source = run_langfuse(args.n)
    else:  # default / --dry-run: offline self-check, no report file
        run_dry_run()
        return
    report(rows, source, args.out)


if __name__ == "__main__":
    main()
