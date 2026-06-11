# debug/ — 디버그 도구 사용 설명서

운영 중 "어떤 세션이 엉뚱한 답을 했다"는 제보가 오면, 이 폴더의 도구로
**어느 모듈 문제인지 귀인하고 → 그 모듈만 떼어 재실행해 검증**합니다.

| 문서 | 역할 |
|------|------|
| **이 문서** (`debug/README.md`) | 도구 조작법 — 명령·옵션·종료코드·실행 환경 (한국어) |
| [`docs/debugging/`](../docs/debugging/README.md) | 증상→원인 런북 — "이런 증상이면 어디를 본다" (영문, 실측 데이터 기반) |

> **시간대 (한 번만 외우면 됩니다):** Langfuse 화면·API는 **UTC**,
> `app.log`·`qa.jsonl`은 **KST(+09:00)**. UTC 07:30 = KST 16:30.

---

## 1. 빠른 시작

### 제일 쉬운 방법 — 대화형 메뉴

```bash
python -m debug
```

번호를 고르고 물어보는 값(트레이스 ID 등)만 입력하면 됩니다. 실행 직전에
동등한 직접 명령(`$ python -m debug.pipeline a687e093 …`)을 보여주므로,
익숙해지면 그 명령을 바로 쓰면 됩니다.

### 직접 실행

```bash
python -m debug.<도구> <인자>        # 예: python -m debug.logs a687e093
python -m debug <도구> <인자>        # 위와 동일 (패스스루)
python -m debug.<도구> --help        # 모든 도구가 자세한 --help 제공
```

### 사전 준비

`project/.env`에 아래 3개가 있어야 Langfuse 조회 도구(status/analyze/
session/pipeline)가 동작합니다 (`project/.env.example` 참조, 키 커밋 금지):

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com    # EU — US로 바꾸면 안 됨
```

`.env`는 어느 폴더에서 실행하든 repo 루트 기준으로 자동 로드됩니다.

---

## 2. 어디서 뭘 돌릴 수 있나 (환경 매트릭스)

| 도구 / 서브커맨드 | dev 박스 (Windows·Mac·WSL) | 운영 박스 (H100) | 필요한 것 |
|---|---|---|---|
| `status` `analyze` `session` `pipeline` | ✅ | ✅ | Langfuse 키 (네트워크) |
| `logs` | ✅ (로그 파일이 있다면) | ✅ | 로그 파일 접근만 — **완전 오프라인** |
| `repro chunk` / `repro parent` | ✅ | ✅ | 앱 임포트만 |
| `repro rewrite` | ✅ (Ollama가 닿으면) | ✅ | `OLLAMA_BASE_URL` |
| `repro search` / `repro answer` | ❌ exit 2로 안내 후 종료 | ✅ | torch + sentence-transformers (**운영 박스 전용**) |

---

## 3. 도구별 레퍼런스

### 3.1 `debug.status` — 서버 한 번 점검 (cron / 작업 스케줄러용)

```bash
python -m debug.status [--server-url URL]
```

5가지를 점검하고 **이상이 있으면 종료코드 1**로 끝납니다 — cron이나 Windows
작업 스케줄러에 걸어 두면 사람에게 알림을 보낼 수 있습니다 (설정 예시는
`--help`와 [status.md](../docs/debugging/status.md) 참조):
`/health` 응답 → 레이턴시 베이스라인(최근 7일 p50 vs 그 전 7일) → 에러 관측
→ 노드 생존성 → **chat-IN 고아 감지** (운영 로그가 주는 유일한 크래시 신호).

| 옵션 / 환경변수 | 의미 |
|---|---|
| `--server-url URL` | 서버 주소 (기본: `$BUFS_SERVER_URL` 또는 `http://localhost:8000`) |
| `BUFS_LOG_DIR` | 고아 감지가 읽을 로그 폴더 (기본: repo의 `logs/`) |

| 종료코드 | 의미 |
|---|---|
| 0 | 모든 점검 통과 |
| 1 | 이상 감지 (어떤 항목인지 STATUS 블록에 출력) |
| 2 | 설정 오류 — `project/.env`의 Langfuse 키 확인 |

> 주의: 점검 시점에 **아직 처리 중인 긴 요청**도 고아(ORPHAN)로 잡힐 수
> 있습니다(290초 폭주 사례 실재). 알림을 받으면 몇 분 뒤 한 번 더 돌려
> 확인하세요.

### 3.2 `debug.analyze` — 운영 전체 통계

```bash
python -m debug.analyze [--last N] [--since ISO] [--node NAME] [--list-nodes] [--errors]
```

트레이스 레이턴시 p50/p90/p95, 노드별 레이턴시·토큰, 에러 집계,
에이전트 루프 깊이 분포를 출력합니다.

| 옵션 | 의미 |
|---|---|
| (없음) | 최근 7일 전체 통계 |
| `--node NAME` | 한 노드의 실행 이력만 (이름은 `--list-nodes`로 확인) |
| `--list-nodes` | 관측된 노드 이름 전체 목록 |
| `--errors` | 비정상(non-DEFAULT) 관측만 |
| `--last N` | 최근 N개 트레이스만 |
| `--since ISO` | 특정 시점 이후 (예: `2026-06-08`) |

종료코드: 0 정상 / 1 키 누락·조회 실패.

→ 느린 트레이스를 찾았으면 그 `tid`로 [3.4 pipeline](#34-debugpipeline--트레이스-1건-단계별-분석)에 넘어갑니다.

### 3.3 `debug.session` — 세션 단위 Q&A 조회

```bash
python -m debug.session <세션UUID | 8-hex tid | 32-hex Langfuse ID>
```

세션 안의 모든 질문·답변을 시간순으로 보여주고, 각 턴에 판정 플래그를 답니다.

| 플래그 | 의미 | 다음 행동 |
|---|---|---|
| `REFUSE` | 검색 span도 도구 호출도 없음 (거절 경로) | [rewrite_query.md](../docs/debugging/rewrite_query.md) |
| `NO-RESULTS` | `num_results=0` (REFUSE 턴에는 둘 다 붙을 수 있음) | [tools-search.md](../docs/debugging/tools-search.md) |
| `SENTINEL` | 검색은 됐는데 답이 "찾지 못했습니다" | [aggregate_answers.md](../docs/debugging/aggregate_answers.md) |
| `RUNAWAY(Ns)` / `RUNAWAY-ANSWER(Nch)` | 60초 초과 / 답변 5000자 초과 | [orchestrator.md](../docs/debugging/orchestrator.md) |
| `ORPHAN` | chat-IN만 있고 chat-OUT 없음 (크래시/중단) | [3.5 logs](#35-debuglogs--로컬-로그-조회-완전-오프라인) + server.err |

종료코드: 0 정상 / 1 조회 실패.

> 알려진 한계: **16자리** Langfuse 트레이스 ID와 세션 UUID **앞부분만** 넣는
> 것은 동작하지 않습니다 — 조용히 0건이 나옵니다 (코드 버그 이슈로 추적 중).
> 전체 UUID, 8-hex tid, 32-hex Langfuse ID 중 하나를 쓰세요.

### 3.4 `debug.pipeline` — 트레이스 1건 단계별 분석

```bash
python -m debug.pipeline <tid> [--raw]
```

요청 1건을 단계별(rewrite → orchestrator → search → aggregate)로 펼치고,
각 단계에 "여기서 잘못되면 이렇게 보인다" 주석과 의심 모듈을 답니다.
`RUNAWAY`(>60s)·`AGENT LOOP`(>4회)·`HIGH LLM CALL COUNT`(>6회) 플래그 자동 표시.

| 옵션 | 의미 |
|---|---|
| `tid` | 8-hex app tid **또는** 32-hex Langfuse ID (자동 해석) |
| `--raw` | 주석 없이 관측 타임라인만 |

종료코드: 0 정상 / 1 조회 실패. 전체 워크플로 예시(290초 폭주 실사례)는
[trace-to-root-cause.md](../docs/debugging/trace-to-root-cause.md).

### 3.5 `debug.logs` — 로컬 로그 조회 (완전 오프라인)

```bash
python -m debug.logs <8-hex tid> [--log-dir DIR]
```

`app.log*`(회전 포함)과 `logs/qa/qa_*.jsonl`을 trace id로 조인해
[chat-IN]/[chat-OUT]/PIPELINE_TIMING/QA 레코드를 한 번에 보여줍니다.
chat-IN만 있으면 **ORPHAN 경고**를 출력합니다.

| 옵션 / 환경변수 | 의미 |
|---|---|
| `tid` | **8자리 소문자 hex만** (예: `a687e093`) — 아니면 exit 2 |
| `--log-dir DIR` / `BUFS_LOG_DIR` | 로그 트리 루트 (기본: repo의 `logs/`) |

종료코드: 0 정상 / 2 tid 형식 오류.

> 알려진 한계: 현재 INFO/WARNING 라인만 파싱하므로 ERROR 레벨의
> `[chat-ERR]` 라인은 출력에 안 나옵니다 (코드 버그 이슈로 추적 중).
> 크래시 포렌식 중이면 `grep chat-ERR logs/backend/app.log*`를 병행하세요.

### 3.6 `debug.repro` — 모듈 단독 재실행

```bash
python -m debug.repro <서브커맨드> [인자]
```

| 서브커맨드 | 무엇을 재실행 | 어디서 | 필요한 것 |
|---|---|---|---|
| `rewrite "<질문>"` | 운영 질의 재작성 체인 (구조화 출력 그대로) | 어디서나 | `OLLAMA_BASE_URL` |
| `search "<쿼리>" [--threshold X] [--db PATH]` | 운영 검색 경로 (HYBRID: bge-m3 + BM25) | **운영 박스** | torch, sentence-transformers |
| `chunk <md파일>` | DocumentChuncker 부모/자식 경계 | 어디서나 | 앱 임포트만 |
| `parent <parent_id>` | parent_store에서 부모 청크 조회 | 어디서나 | 앱 임포트만 |
| `answer "<질문>"` | e2e RAG 답변 1회 (**비결정적**) | **운영 박스** | torch + Ollama |

`search`는 매 실행마다 **인덱스 지문**(`meta=… sqlite=[…] git=…`)을 출력합니다 —
커밋된 인덱스와 운영 인덱스가 어긋났는지 즉시 보입니다. `--threshold`는
관련성 게이트를 격리해서 흔드는 레버이고, 각 청크에 PASS/FAIL이 붙습니다.
운영 서버가 켜져 있어도 안전합니다 (인덱스를 임시 폴더로 복사해서 엽니다).

종료코드: 0 정상 / 1 환경변수·의존성 누락, 실행 실패 / 2 운영 박스 전용
의존성 누락 (dev 박스에서 `search`/`answer` 실행 시).

> 알려진 한계: `search`의 `k` 값이 운영과 다르게 잡히는 버그가 있습니다
> (운영은 LLM이 지정한 limit≈5–7, repro는 `MAX_TOOL_CALLS` 사용 — 코드 버그
> 이슈로 추적 중). 결과 **집합**을 운영과 비교할 때는 이 점을 감안하세요.

---

## 4. 트레이스 ID 두 가지

| 형식 | 길이 | 예 | 어디서 복사하나 |
|---|---|---|---|
| app tid | 8-hex | `a687e093` | `app.log`·`qa.jsonl`·사용자 제보, `debug.analyze` 출력의 `tid=` |
| Langfuse 트레이스 ID | 32-hex | `51c47a50…07e3` | Langfuse 웹 UI의 트레이스 URL |

`pipeline`과 `session`은 둘 다 받습니다. `logs`는 8-hex만 받습니다
(아니면 exit 2).

---

## 5. 대화형 메뉴 상세 (`python -m debug`)

| 번호 | 도구 | 물어보는 것 |
|---|---|---|
| 1 | `debug.status` | 서버 URL (엔터=기본) |
| 2 | `debug.analyze` | 하위 메뉴: 전체 / 노드별 / 노드 목록 / 에러만 / 최근 N건 |
| 3 | `debug.pipeline` | tid, raw 여부 |
| 4 | `debug.session` | 세션 UUID 또는 tid |
| 5 | `debug.logs` | tid, 로그 폴더 (엔터=기본) |
| 6 | `debug.repro` | 하위 메뉴: rewrite/search/chunk/parent/answer + 각 인자 |
| q | 종료 | |

- 입력 중 **빈 입력 또는 `b`** = 메뉴로 돌아가기. **Ctrl-C** = 어디서든
  (입력 중이든 도구 실행 중이든) 메뉴로 돌아갑니다. 종료는 `q` 또는 Ctrl-D.
- 실행 후 종료코드와 해석(예: `status` 1 = 이상 감지)을 보여줍니다.
- 메뉴는 각 도구를 별도 프로세스로 실행하므로, cron 등에 직접 거는 경우와
  종료코드가 완전히 동일합니다. 패스스루(`python -m debug <도구> …`)는
  출력에 아무것도 덧붙이지 않아 파이프에 안전합니다.

---

## 6. 자주 나는 오류

| 증상 | 원인 / 해결 |
|---|---|
| `missing environment variable(s): LANGFUSE_…` (exit 1·2) | `project/.env`에 키 3종 추가 (§1 사전 준비) |
| Langfuse 조회는 되는데 트레이스가 0건 | `LANGFUSE_BASE_URL`이 EU(`cloud.langfuse.com`)인지 확인 — US 엔드포인트엔 데이터가 없음 |
| `ERROR: missing production deps: torch…` (exit 2) | `repro search/answer`는 운영 박스 전용 — dev에서는 정상 동작 |
| `tid must be exactly 8 lowercase hex chars` (exit 2) | `debug.logs`는 8-hex만. 32-hex밖에 없으면 `debug.pipeline`으로 metadata의 tid를 먼저 확인 |
| 시간이 9시간 어긋나 보임 | Langfuse=UTC, app.log·qa.jsonl=KST. 정상입니다 |
| Windows에서 출력 글자 깨짐 | 도구가 stdout을 UTF-8로 재설정하지만, 리다이렉트 파일을 열 때는 UTF-8로 여세요 |

---

## 7. 테스트

```bash
python -m pytest tests/debug/ -q
```

전부 오프라인입니다 — Langfuse 키·네트워크·torch 불필요. 실 운영 로그
발췌 픽스처(`tests/fixtures/logs/`) 기반 파서 테스트 + import 순수성
트립와이어 + 런처 테스트.

---

## 8. 더 깊이

- 증상에서 출발하려면: [`docs/debugging/README.md`](../docs/debugging/README.md) (런북 인덱스, 영문)
- 전 과정 워크스루 (290초 폭주 실사례): [`trace-to-root-cause.md`](../docs/debugging/trace-to-root-cause.md)
- 운영 박스 콜드스타트 (.venv 만들기부터): [`docs/debugging/README.md` Quickstart](../docs/debugging/README.md#quickstart-cold-start-on-the-production-server)
