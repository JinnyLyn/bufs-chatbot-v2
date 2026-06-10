# 테스트 가이드 (BUFS Chatbot v2)

## 빠른 시작

```bash
# 테스트 의존성 설치 (최소 서브셋, torch 없음)
pip install "pytest>=8.3" "pytest-asyncio>=0.24" "pytest-cov>=6.0" \
  pydantic "langgraph==1.2.0" "langchain-text-splitters==1.1.2" \
  "tiktoken==0.13.0" "pymupdf4llm==1.27.2.3"

# 오프라인 단위 테스트 실행 (기본값)
pytest

# 커버리지 포함
pytest -m "not integration" --cov=project --cov-report=term

# 통합 테스트만 (로컬 Ollama/Qdrant 필요)
pytest -m integration
```

---

## 테스트 분류 체계

### 단위 테스트 (`-m unit` / 기본값)

오프라인. 네트워크 없이 실행. Ollama/Qdrant/Langfuse 불필요.

| 테스트 파일 | 대상 모듈 | 핵심 검증 항목 |
|---|---|---|
| `tests/test_config.py` | `project/config.py` | 환경 변수 파싱, 기본값, `LANGFUSE_BASE_URL→LANGFUSE_HOST` 미러링, OLLAMA 0.0.0.0→127.0.0.1 재작성 |
| `tests/rag_agent/test_edges.py` | `rag_agent/edges.py` | `route_after_rewrite` (불명확→재확인, 명확→Send 리스트), `route_after_orchestrator_call` 경계 조건 |
| `tests/rag_agent/test_schemas.py` | `rag_agent/schemas.py` | `QueryAnalysis` Pydantic 검증 및 직렬화 |
| `tests/rag_agent/test_graph_state.py` | `rag_agent/graph_state.py` | `accumulate_or_reset` (__reset__ 센티널), `set_union` 리듀서, State/AgentState 필드 |
| `tests/rag_agent/test_prompts.py` | `rag_agent/prompts.py` | 모든 프롬프트 함수 반환 검증, 핵심 키워드 포함 여부 |
| `tests/test_document_chunker.py` | `document_chunker.py` | 부모/자식 청킹, 학번 섹션 하드 경계, 연도 범위 확장, 월 컬럼 전방 채우기 |
| `tests/api/test_trace_context.py` | `api/trace_context.py` | 8-hex `new_trace_id`, ContextVar get/set, `TraceFilter` LogRecord 주입 |
| `tests/test_utils.py` | `utils.py` | `estimate_context_tokens`, `clear_directory_contents` (tmp_path 사용) |
| `tests/api/test_qa_logger.py` | `api/qa_logger.py` | JSONL 레코드 구조, 날짜별 파일명, `CHAT_LOG_DISABLED` / `X-Test-Mode` 스킵 플래그, 읽기 헬퍼 |

**픽스처** (`tests/conftest.py`):

| 픽스처 | 용도 |
|---|---|
| `env_isolated` | 모든 `LANGFUSE_*` / `LLM_*` / `OLLAMA_*` 환경 변수를 제거하고 config를 리로드 |
| `fake_llm` | 도구 호출 없는 기본 응답을 반환하는 호출 가능한 LLM 스텁 |
| `fake_llm_with_tool_call` | 첫 번째 응답에 tool_call이 포함된 LLM 스텁 |
| `fake_vector_store` | `similarity_search_with_score` → `[]`를 반환하는 MagicMock |
| `fake_parent_store` | 딕셔너리 기반 부모 문서 스토어 (실제 파일시스템 없음) |
| `fake_langfuse_handler` | Langfuse 자격 증명 없이 no-op 콜백을 제공하는 MagicMock |

### 통합 테스트 (`-m integration`)

실제 서비스나 파일시스템 I/O가 필요. 기본적으로 **비활성화** (CI 기본 실행에서 제외).
환경이 충족되지 않으면 개별 테스트가 **자동 스킵**됨.

| 테스트 파일 | 대상 모듈 | 필요 조건 |
|---|---|---|
| `tests/db/test_parent_store_manager.py` | `db/parent_store_manager.py` | 오프라인 가능 (파일시스템만 사용) |
| `tests/db/test_vector_db_manager.py` | `db/vector_db_manager.py` | `qdrant-client`, `langchain-qdrant`, `langchain-huggingface` 설치 필요 |
| `tests/rag_agent/test_tools.py` | `rag_agent/tools.py` | 가짜 스토어 사용 (오프라인 가능), 그래프 컴파일 확인 포함 |
| `tests/api/test_chat_integration.py` | `api/chat.py`, `api/agent_stream.py` | 가짜 RAG 테스트는 오프라인, 실제 LLM 테스트는 `OLLAMA_BASE_URL` 필요 |
| `tests/test_live_llm.py` | 실 Ollama 엔드포인트 | `OLLAMA_BASE_URL` + `langchain-ollama` 필요 |

#### 통합 테스트 실행 방법

```bash
# 통합 테스트 전용 extras 설치 (requirements-dev.txt에 포함되지 않음)
pip install langchain-ollama==1.1.0

# 모든 통합 테스트 (서비스 미설정 시 대부분 스킵)
pytest -m integration -v

# 서비스 환경 변수 설정 후 실행 (실 LLM 포함)
OLLAMA_BASE_URL=http://127.0.0.1:11434 \
LLM_MODEL=qwen3.5:9b \
QDRANT_DB_PATH=./qdrant_db \
pytest -m integration -v
```

> **참고**: `pytest -m integration` 실행 시 테스트가 하나도 수집되지 않으면 pytest는
> **exit code 5**를 반환합니다 (테스트 없음). CI에서 이를 성공으로 처리하려면
> `|| true` 또는 `--ignore-glob` 등을 활용하세요.

---

### requirements-dev.txt에 torch/sentence-transformers가 없는 이유

`requirements.txt`의 `sentence-transformers`와 `torch`는 4GB+ 다운로드가 필요하며,
단위 테스트 대상 8개 모듈(`config.py`, `edges.py`, `schemas.py` 등)은 임베딩 모델에
의존하지 않습니다. 또한 Python 3.14 환경에서는 바이너리 호환성 문제가 발생할 수 있어
최소 서브셋만 설치합니다. 전체 requirements는 프로덕션 설치(`pip install -r requirements.txt`)
시 그대로 사용하세요.

---

## eval_tools/가 별도인 이유

`eval_tools/` 디렉터리의 스크립트들은 실제 RAG 시스템, Langfuse, 또는 실제
마크다운 문서에 의존합니다. 이 스크립트들은:

1. **비결정적** — 실제 LLM 응답에 따라 결과가 달라집니다
2. **라이브 서비스 의존** — Ollama 서버, Qdrant DB, 실제 임베딩 모델이 필요합니다
3. **평가 목적** — 품질 측정용이며 회귀 방지용 CI 게이트가 아닙니다

이 스크립트들을 pytest 테스트로 만들면 **허위 CI 신호**가 발생하므로 분리합니다.
CI는 오프라인 단위 테스트만 게이팅합니다. 평가는 `python eval_tools/...`로
별도 실행하세요.

---

## config.py 주의사항 (import-time 부작용)

`project/config.py`는 **임포트 시점**에 환경 변수를 읽어 모듈 레벨 상수로
저장합니다. 환경 변수를 변경하는 테스트는 반드시:

1. `monkeypatch.setenv()`로 변경 **후**
2. `importlib.reload(sys.modules["config"])`로 재로드해야 합니다

`env_isolated` 픽스처가 이 패턴을 자동화합니다. `from config import X` 형태로
임포트한 모듈은 재로드 후에도 오래된 바인딩을 유지하므로, 항상 `cfg.X`로
접근하세요.
