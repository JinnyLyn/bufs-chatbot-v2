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
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                    # import qa_scorer / error_taxonomy / kpi.*
sys.path.insert(0, os.path.dirname(_HERE))   # kpi/runners 내부의 `eval_tools.*` 절대 임포트용
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
        ranked_out=None,               # --probe-embedding 후처리로 채움
        hallucinated=hallucinated,
    )
    return sig, diag


# --------------------------------------------------------------------------- 후처리 신호 (B)

def enrich_judge(rows: list[dict], url: str, model: str) -> None:
    """②/⑤ 분리: GENERATION_UNSPLIT 행에 RAGAS faithfulness judge(Ollama)를 물려 재분류.

    judge 프롬프트/파서는 :mod:`kpi.runners.ragas` 의 검증된 것을 그대로 임포트해 쓴다.
    score < FAITH_THRESHOLD → ⑤ Hallucination, 이상 → ② Prompt 실패. 파싱 실패(-1)는
    미분리 유지(추측 금지). in-place 갱신, 실패는 행 단위로 건너뜀.
    """
    from kpi.runners.ragas import (
        JUDGE_ANSWER_CHARS, JUDGE_CONTEXT_CHARS, _METRIC_CONFIG, _extract_score, _ollama_judge,
    )
    system, template = _METRIC_CONFIG["faithfulness"]
    targets = [r for r in rows if r["bucket"] == "GENERATION_UNSPLIT" and r.get("_ctx")]
    print(f"[judge] GENERATION_UNSPLIT {len(targets)}행에 faithfulness judge({model}) 적용...")
    failed = unparsed = 0
    for r in targets:
        try:
            # 트런케이션 한도는 kpi 러너와 공유 상수 — 두 경로의 점수 비교 가능성 유지
            out = _ollama_judge(system, template.format(context=r["_ctx"][:JUDGE_CONTEXT_CHARS],
                                                        answer=r["_answer"][:JUDGE_ANSWER_CHARS]),
                                url=url, model=model)
            score, reason = _extract_score(out)
        except Exception as e:  # noqa: BLE001 — 행 단위 실패는 미분리 유지가 정직
            failed += 1
            r["diag"]["judge"] = f"호출 실패: {e}"
            print(f"  id={r['id']}: judge 호출 실패({e}) — 미분리 유지")
            continue
        if score < 0:
            unparsed += 1
            r["diag"]["judge"] = "unparseable — 미분리 유지"
            continue
        v = classify(replace(r["_sig"], hallucinated=score < FAITH_THRESHOLD))
        r["diag"]["judge_faithfulness"] = score
        if reason:
            r["diag"]["judge_reason"] = reason
        r["bucket"], r["reason"] = v.bucket, v.reason
    if targets and (failed + unparsed) == len(targets):
        print(f"[warn] judge 전체 실패(호출실패 {failed} + 파싱실패 {unparsed} = {len(targets)}) — "
              f"②/⑤ 미분리 그대로. 엔드포인트({url})/모델({model})/think 지원 여부를 확인하세요.")


def enrich_embedding_probe(rows: list[dict], corpus: KBCorpus, k: int = 10) -> None:
    """①→⑦ 분리: SEARCH_FAILURE 행의 질문을 **dense-only** top-k로 검색해 임베딩 레그를 격리.

    빠진 사실이 단일 청크에 통째로 있는데(≒ SEARCH_FAILURE 전제) 그 청크가 dense top-k에
    없으면 ranked_out=True → ⑦ Embedding 문제. top-k에 있으면 dense 레그는 무죄 —
    ①로 남기고 사유에 '하이브리드/재작성 단계 손실'을 명시한다.

    임베디드 Qdrant(단일 프로세스 락)라 **백엔드가 내려가 있어야** 한다. 락/의존성 실패 시
    경고만 내고 전체를 건너뛴다(행은 ①로 정직하게 유지).
    """
    targets = [r for r in rows if r["bucket"] == "SEARCH_FAILURE"]
    if not targets:
        print("[probe] SEARCH_FAILURE 행 없음 — 임베딩 프로브 생략")
        return
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        from langchain_qdrant import QdrantVectorStore, RetrievalMode
        from qdrant_client import QdrantClient
        sys.path.insert(0, os.path.join(et._ROOT, "project"))
        import config as app_config
    except Exception as e:  # noqa: BLE001 — 의존성 미설치 등
        print(f"[probe] 임베딩 프로브 의존성 로드 실패({type(e).__name__}: {e}) — ① 유지")
        return
    client = None
    try:
        client = QdrantClient(path=app_config.QDRANT_DB_PATH)
        dense = HuggingFaceEmbeddings(model_name="BAAI/bge-m3", model_kwargs={"device": "cpu"})
        store = QdrantVectorStore(client=client, collection_name=app_config.CHILD_COLLECTION,
                                  embedding=dense, retrieval_mode=RetrievalMode.DENSE)
        print(f"[probe] SEARCH_FAILURE {len(targets)}행에 dense-only top-{k} 프로브 적용...")
        probe_failed = 0
        for r in targets:
            question = r.get("_q", "")
            if not question:
                continue
            try:
                docs = [d.page_content for d in store.similarity_search(question, k=k)]
            except Exception as e:  # noqa: BLE001 — 행 1건 실패로 전체(비싼 fetch) 중단 방지
                probe_failed += 1
                r["diag"]["probe"] = f"검색 실패: {e}"
                print(f"  id={r['id']}: 프로브 검색 실패({type(e).__name__}: {e}) — ① 유지")
                continue
            topk = "\n".join(docs)
            # 단일 청크에 통째로 존재하는 빠진 사실 중, dense top-k가 놓친 것
            probed = [f for f in r["diag"].get("missing", []) if corpus.fact_in_single_chunk(f)]
            missed = [f for f in probed if not present(f, topk)]
            if not probed:
                continue
            r["diag"]["dense_topk_missed"] = missed
            if missed:
                v = classify(replace(r["_sig"], ranked_out=True))
                r["bucket"], r["reason"] = v.bucket, v.reason
            else:
                r["reason"] = "dense top-k엔 있음 — 하이브리드 융합/질의 재작성 단계에서 손실(①)"
        if probe_failed:
            print(f"[warn] 프로브 검색 실패 {probe_failed}/{len(targets)}행 — 해당 행은 ①로 유지됨")
    except Exception as e:  # noqa: BLE001 — 락(백엔드 실행 중)/모델 로드 실패 등
        print(f"[probe] 임베딩 프로브 불가({type(e).__name__}: {e}) — ① 유지. "
              f"백엔드를 내리고 다시 실행하면 ⑦ 분리 가능")
    finally:
        if client is not None:
            client.close()  # 예외 경로에서도 임베디드 Qdrant 락 해제 (락 파일 누수 방지)


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
    print(f"\n{'-'*72}\n카테고리 × 버킷 (정답/①검색/②Prompt/③문서없음/⑤Hallu/⑥Chunk/⑦Embed/미분리):")
    for cat, cc in sorted(by_cat.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(cc.values())
        print(f"  {cat:10} n={tot:2}  ok={cc['CORRECT']:2} ①={cc['SEARCH_FAILURE']:2} "
              f"②={cc['PROMPT_FAILURE']:2} ③={cc['NO_DOCUMENT']:2} ④={cc['AMBIGUOUS_QUESTION']:2} "
              f"⑤={cc['HALLUCINATION']:2} ⑥={cc['CHUNK_PROBLEM']:2} ⑦={cc['EMBEDDING_PROBLEM']:2} "
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
        # 내부 상태(_sig 등 비직렬화 값)는 리포트에서 제외
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
    }
    out_dir = os.path.dirname(out_path)
    if out_dir:  # 순수 파일명(--out result.json)이면 makedirs('') 크래시 방지
        os.makedirs(out_dir, exist_ok=True)
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
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        sys.exit(f"[error] 덤프 파일을 찾을 수 없습니다: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"[error] 덤프 JSON 파싱 실패 ({path}): {e}")
    except OSError as e:
        sys.exit(f"[error] 덤프 파일을 열 수 없습니다 ({path}): {e}")
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
                     "bucket": v.bucket, "reason": v.reason, "diag": diag,
                     "_sig": sig, "_answer": answer, "_ctx": ctx or "",
                     "_q": str(obs.get("question") or gold.get("question") or "")})
        if n and len(rows) >= n:
            break
    first_rec = recs[0] if recs else {}   # 명시적 방어 — 단락 평가 의존 제거
    has_ctx = any(k in first_rec for k in ("context_preview", "contexts", "context"))
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
    missing_env = [k for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not os.environ.get(k)]
    if not base or missing_env:
        sys.exit(f"[error] Langfuse 설정 누락: LANGFUSE_BASE_URL={'OK' if base else '없음'}, "
                 f"누락 키={missing_env or '없음'} — project/.env 확인 (--langfuse 모드 필수)")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    ca = os.environ.get("REQUESTS_CA_BUNDLE")

    def fetch(path, want, **params):
        out, page = [], 1
        while len(out) < want:
            data = None
            last_err = None
            for a in range(6):
                try:
                    r = requests.get(base + path, params={"limit": 50, "page": page, **params},
                                     auth=auth, timeout=60, verify=ca)
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {r.status_code}"; time.sleep(2 * (a + 1)); continue
                    r.raise_for_status()
                    body = r.json()  # ValueError(비JSON)는 아래 except에서 재시도
                    data = body.get("data") if isinstance(body, dict) else None
                    if data is None:
                        # 200인데 스키마 상이(API 변경 등) — 재시도해도 같으므로 즉시 중단+경고
                        print(f"[warn] Langfuse {path} 응답에 'data' 키 없음"
                              f"(keys={list(body)[:5] if isinstance(body, dict) else type(body).__name__})"
                              f" — 수집 중단, 결과가 부분집합일 수 있음")
                        return out[:want]
                    break
                except (requests.exceptions.RequestException, ValueError) as e:
                    last_err = repr(e); time.sleep(2 * (a + 1))
            if data is None and last_err:
                # 재시도 소진 — 무음 부분 반환 금지: 페이지 유실을 명시적으로 알린다
                print(f"[warn] Langfuse {path} page={page} 재시도 6회 실패({last_err}) — "
                      f"이후 페이지 생략, 결과가 부분집합일 수 있음")
            if not data:
                break
            out.extend(data); page += 1
        return out[:want]

    def text(v):
        if isinstance(v, dict):
            # chat-turn root span shape: {"question": ...} / {"answer": ...}
            if "question" in v:
                return str(v["question"])
            if "answer" in v:
                return str(v["answer"])
            msgs = v.get("messages") or []
            if msgs and isinstance(msgs[-1], dict):
                return str(msgs[-1].get("content", ""))
            return str(v.get("output") or v)
        return str(v or "")

    print("pulling traces + tool observations from Langfuse...", flush=True)
    traces = fetch("/api/public/traces", 260)
    if not traces:
        sys.exit("[error] Langfuse에서 트레이스 0건 — 키/URL이 잘못됐거나(위 [warn] 확인) "
                 "새 프로젝트에 아직 실행 기록이 없음. 빈 리포트를 쓰지 않고 중단합니다.")
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
                     "bucket": v.bucket, "reason": v.reason, "diag": diag,
                     "_sig": sig, "_answer": answer, "_ctx": ctx or "", "_q": q})
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
    ap.add_argument("--judge", metavar="MODEL", default=None,
                    help="②/⑤ 분리: GENERATION_UNSPLIT에 faithfulness judge 적용 (예: qwen3.5:9b)")
    ap.add_argument("--judge-url", default=os.environ.get("OLLAMA_JUDGE_URL", "http://127.0.0.1:11434"),
                    help="judge Ollama 엔드포인트 (기본: $OLLAMA_JUDGE_URL 또는 H100 로컬 :11434)")
    ap.add_argument("--probe-embedding", action="store_true",
                    help="①→⑦ 분리: dense-only top-k 프로브 (임베디드 Qdrant — 백엔드 내린 상태에서)")
    ap.add_argument("--k", type=int, default=10, help="임베딩 프로브 top-k")
    args = ap.parse_args()

    if args.from_dump:
        rows, source = run_from_dump(args.from_dump, args.n)
    elif args.langfuse:
        rows, source = run_langfuse(args.n)
    else:  # default / --dry-run: offline self-check, no report file
        run_dry_run()
        return
    if args.judge:
        enrich_judge(rows, args.judge_url, args.judge)
        source += f" +judge:{args.judge}"
    if args.probe_embedding:
        enrich_embedding_probe(rows, KBCorpus.from_repo(), k=args.k)
        source += f" +probe:k{args.k}"
    report(rows, source, args.out)


if __name__ == "__main__":
    main()
