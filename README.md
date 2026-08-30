<h1 align="center">부산외대 학사 챗봇 (BUFS Academic Chatbot)</h1>

<p align="center">
  <strong>agentic-RAG 코어 × CamChat 채팅 UI — 부산외국어대학교 학사 질의응답 챗봇</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/LangGraph-agentic-orange?logo=langchain&logoColor=white" alt="LangGraph"/>
  <img src="https://img.shields.io/badge/Qdrant-hybrid-DC244C" alt="Qdrant"/>
  <img src="https://img.shields.io/badge/LLM-qwen3.5%3A9b-purple" alt="LLM"/>
  <img src="https://img.shields.io/badge/frontend-Next.js-black?logo=nextdotjs" alt="Next.js"/>
</p>

---

## 개요

두 프로젝트를 융합한 한국어 학사 Q&A 챗봇이다.

- **검색→생성 파이프라인 = agentic-RAG**: [agentic-rag-for-dummies](https://github.com/GiovanniPasq/agentic-rag-for-dummies)의 LangGraph 멀티스텝 에이전트(`project/`)를 그대로 사용하고, FastAPI SSE 브리지(`project/server.py`, `project/api/`)로 감쌌다.
- **프론트엔드 = CamChat**: 기존 `bufs-chatbot`의 Next.js 채팅 UI만 가져와 **핵심 채팅 기능으로 축소**(관리자·성적표·로그인 등 부가기능 제외).

부산외대 2026학년도 1학기 학사문서 23종을 지식베이스로, 학번별 졸업요건·학사일정·수강신청·장학 등에 답한다.

## 아키텍처

```
[Next.js 채팅 UI :3000]  ──SSE──▶  [FastAPI :8000]  ──▶  LangGraph agentic 그래프
                                                          rewrite_query
                                                          └─ agent (orchestrator ↔ search/parent ↔ compress)
                                                          └─ aggregate_answers
                                                          ├─ LLM: qwen3.5:9b (Ollama, H100/로컬 GPU)
                                                          └─ 검색: Qdrant 하이브리드(dense bge-m3 + sparse BM25, RRF)
```

| 구성 | 내용 |
|---|---|
| **LLM** | `qwen3.5:9b` (Ollama). 원격 **H100**(SSH 터널 `:11434`, ~59 tok/s) 또는 로컬 RTX 4070(`:11435`). thinking off. |
| **검색** | Qdrant 임베디드, dense `BAAI/bge-m3`(CPU) + sparse `Qdrant/bm25`, RRF 하이브리드 |
| **청킹** | parent/child 계층 + **학번 섹션 인지** + **학사일정 '월' 컬럼 채우기**(`project/document_chunker.py`) |
| **지식베이스** | 학사문서 23종 → parent 194 / child 1,459 |
| **관측** | Langfuse Cloud(전 요청 추적) + 회전 파일로그 + Q&A JSONL + `/health` |
| **배포** | Cloudflare 터널(`maruvis.kr` → 프론트 :3000 / `/api/*` → 백엔드 :8000, same-origin). H100 리눅스 서버, [MIGRATION_H100.md](MIGRATION_H100.md) 참조 |

## BUFS 특화 개선

원본 agentic-RAG를 실제 학사 데이터에 맞추며 추가한 것들:

- **학번 섹션 청킹** — "2020학번 졸업요건"이 인접 학번과 섞이던 문제 해결. `## YYYY학번` 헤더를 하드 청크 경계로.
- **연도 펼치기** — "2020학번" 질의가 "2017~2020학번" 범위 헤더에 매칭되도록 헤더에 연도 enumerate.
- **졸업학점 합산금지 프롬프트** — 구성요소를 합산해 총 졸업학점을 잘못 만들지 않고 명시값을 인용.
- **학사일정 월 채우기** — rowspan 평탄화로 비어버린 '월' 칸을 채워 "기말고사 6/8"을 "5/8"로 읽던 오류 제거.
- **빠른거부(fast-refuse)** — 문서 범위 밖 질문은 즉시 간결 거부(290초 폭주 → ~10초).
- **하이브리드 점수 임계값** 0.7→0.3 (RRF rank score 대응), **클린 리인덱스**(유령 청크 제거).

## 성능 (평가 결과)

`bufs-chatbot`의 `combined88`(89문항: 답변가능 81 + 문서밖 8)로 룰기반 + RAGAS + Langfuse 분석. 자세한 내용은 [REPORT_결과.md](REPORT_결과.md) · [REPORT_vs_BUFS.md](REPORT_vs_BUFS.md).

| 지표 | **신규 (H100+무압축+빠른거부)** | 기존 BUFS-CHATBOT |
|---|---|---|
| 룰기반 contains 정답 | **85.2%** | 80.3% |
| 문서밖 거부율 | **8/8 (100%)** | 4/8 (50%) |
| RAGAS 종합 (exaone judge) | 0.70 | 0.73 (대등) |
| 검색 정밀도 / 재현율 | 0.63 / 0.68 | 0.41 / 0.78 |
| **평균 응답속도** | 10.8초 (max 28.6초) | **1.8초** |

> **정확도·문서밖 거부·검색 정밀도는 신규 우위, 응답속도는 bufs 우위.** agentic 멀티스텝(LLM 4~7회 호출)이 정확도의 원천이자 지연의 원인 — 기존은 단일패스(LLM 1회)라 빠르지만 깊이가 얕다.

## 빠른 시작

> KB(`markdown_docs`/`parent_store`/`qdrant_db`)가 레포에 포함돼 있어 클론 후 바로 실행 가능. 문서를 바꿨다면 `python project/reindex.py`로 재빌드.

**1) Ollama + 모델**
```bash
# 로컬 GPU
OLLAMA_HOST=127.0.0.1:11435 ollama serve   # 별도 터미널
ollama pull qwen3.5:9b
# 또는 원격 H100: ssh -N -L 11434:localhost:11434 <user>@<host>
```

**2) 환경설정** — `project/.env.example`를 `project/.env`로 복사 후 키 입력
```ini
OLLAMA_BASE_URL=http://127.0.0.1:11435   # 또는 H100 :11434
LLM_MODEL=qwen3.5:9b
LLM_NUM_CTX=8192                          # H100면 16384 + BASE_TOKEN_THRESHOLD=12000
LANGFUSE_ENABLED=true                     # 선택, 키 입력 시
```

**3) 백엔드**
```bash
pip install -r requirements.txt
python project/reindex.py     # (선택) KB 재빌드
python project/server.py      # FastAPI :8000
```

**4) 프론트엔드**
```bash
cd frontend
npm install
npm run dev                   # :3000 (프로덕션: npm run build && npm start)
```
→ http://localhost:3000

전체 통합·운영 가이드는 [INTEGRATION.md](INTEGRATION.md) 참조.
**H100 리눅스 서버로의 서빙 이관**(cloudflared 터널 포함)은 [MIGRATION_H100.md](MIGRATION_H100.md) 참조
— `scripts/start-all.sh` / `stop-all.sh` / `healthcheck.sh`가 기존 `.ps1`들의 리눅스 대응이다.

## 프로젝트 구조

```
.
├── project/                  # agentic-RAG 백엔드 (FastAPI + LangGraph)
│   ├── server.py             # FastAPI SSE 엔트리포인트
│   ├── config.py             # 모델·청킹·검색·관측 설정 (env 기반)
│   ├── api/                  # SSE 브리지, 세션, health, 로깅, trace
│   ├── rag_agent/            # LangGraph 그래프·노드·프롬프트·툴
│   ├── core/ db/             # RAGSystem, Qdrant/parent_store 매니저
│   ├── document_chunker.py   # 학번/월 인지 청킹
│   └── reindex.py            # KB 클린 재빌드
├── frontend/                 # Next.js 채팅 UI (CamChat 축소판)
├── markdown_docs/            # 학사문서 23종 (KB 소스)
├── qdrant_db/ parent_store/  # 벡터DB + parent 청크 (커밋됨)
├── eval_tools/               # 평가/분석 하니스 (룰기반·RAGAS·Langfuse)
├── scripts/                  # 기동/중지/헬스체크(.ps1=Windows, .sh=Linux)/롤백/배포
├── REPORT_결과.md / REPORT_vs_BUFS.md   # 평가·비교 보고서
└── INTEGRATION.md            # 통합·운영 가이드
```

## 평가 재현

골든 데이터셋은 레포에 포함된 `eval_tools/datasets/qa_dataset.json`(100문항)이다.

```bash
python eval_tools/_eval_qa100.py                                  # ⭐ 1순위 룰기반 100문항 (in-repo 골든셋)
python eval_tools/_eval_qa100.py --dry-run                        # 오프라인 데이터셋 검증·통계 (백엔드 불필요)
python eval_tools/_ragas_eval.py --judge ollama --model exaone3.5:7.8b --n 25   # RAGAS
python eval_tools/_langfuse_analyze.py                             # Langfuse 지연·에러 집계
python eval_tools/_answer_analysis.py                             # 정답/오답 검색vs생성 귀인
python eval_tools/_eval_combined88.py                             # (레거시) bufs 89문항, 레포 밖 경로 의존
```

## 기반 / 라이선스

- 코어: [agentic-rag-for-dummies](https://github.com/GiovanniPasq/agentic-rag-for-dummies) (MIT) — LangGraph agentic RAG 베이스
- 프론트: bufs-chatbot(CamChat)의 채팅 UI
- 라이선스: [LICENSE](LICENSE)

> 시크릿(`project/.env`, `*.pem`)·백업·로그·`node_modules`는 `.gitignore`로 제외. 환경변수 형식은 `project/.env.example` 참고.
