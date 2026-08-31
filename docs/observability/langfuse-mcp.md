# Langfuse 연결 설정 가이드

BUFS Chatbot v2의 Langfuse 관측 연결을 설정하는 방법을 설명합니다.
SDK가 주 연결(모든 수락 기준), community MCP는 선택적 인터랙티브 편의 도구입니다.

---

## 1. SDK 연결 (필수)

### 1-1. Langfuse Cloud EU 프로젝트 생성

1. <https://cloud.langfuse.com> 에서 계정·프로젝트 생성
2. **Settings → API Keys** 에서 Public Key / Secret Key 발급

### 1-2. `project/.env` 설정

```ini
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=<여기에-Public-Key>
LANGFUSE_SECRET_KEY=<여기에-Secret-Key>
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # EU. US는 https://us.cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development       # 대시보드 오염 방지: 서버는 production
```

> `LANGFUSE_TRACING_ENVIRONMENT`는 SDK가 직접 읽는 트레이스 environment 속성입니다
> (`production`/`staging`/`development`). 개발 머신과 운영 서버를 다르게 설정해
> 테스트 트레이스가 운영 대시보드에 섞이지 않게 합니다.

> **중요:** 정확한 변수명은 `LANGFUSE_BASE_URL` — `project/config.py`의 Langfuse 블록이
> 이를 SDK가 읽는 `LANGFUSE_HOST`로 미러링합니다. 이 변수가 없으면 SDK가 기본
> 호스트(localhost)로 떨어집니다.

`project/.env`는 `.gitignore`에 의해 추적 제외됩니다:

```bash
git check-ignore project/.env   # exit 0 확인
```

### 1-3. SDK 클라이언트 검증

```bash
cd <repo-root>
.venv/bin/python debug/langfuse_client.py
```

성공 시 출력:

```
auth_check() OK
trace id=<id>  latency=…s  sess=…
```

---

## 2. MCP 서버 설정 (선택)

Claude Code 에이전트가 인터랙티브하게 Langfuse를 조회할 수 있게 합니다.
**MCP 연결 실패는 수락 기준이 아닙니다** — SDK 클라이언트가 모든 필수 기능을 담당합니다.

### 2-1. `.mcp.json` (이미 커밋됨)

```json
{
  "mcpServers": {
    "langfuse": {
      "command": "uvx",
      "args": ["langfuse-mcp"],
      "env": {
        "LANGFUSE_PUBLIC_KEY": "${LANGFUSE_PUBLIC_KEY}",
        "LANGFUSE_SECRET_KEY": "${LANGFUSE_SECRET_KEY}",
        "LANGFUSE_HOST": "${LANGFUSE_BASE_URL}"
      }
    }
  }
}
```

**env-expansion 전용** — 키 리터럴 없음. `uvx`는 PATH에 있어야 합니다
(예: `/home/<사용자>/.local/bin/uvx` — 경로는 환경마다 다릅니다;
필요 시 `.mcp.json`의 `"command"` 값을 절대경로로 변경하세요).

### 2-2. 실행 시 env 주입

`.mcp.json`의 `${VAR}` 는 Claude Code가 시작 시 쉘 환경에서 확장합니다.
**반드시** `project/.env` 값을 쉘에 export하거나 `direnv`/`dotenv-cli`로 로드한 뒤 Claude Code를 시작하세요:

```bash
export $(grep -v '^#' project/.env | xargs)
claude
```

또는 `direnv`를 사용:

```bash
# .envrc
dotenv project/.env
```

### 2-3. 제거

MCP를 제거하려면 `.mcp.json`의 `langfuse` 항목을 삭제하면 됩니다.
SDK 클라이언트(`debug/langfuse_client.py`)는 MCP와 독립적으로 동작합니다.

---

## 3. WSL/Windows CA 번들 주의사항

Norton 등 AV가 HTTPS를 자체 루트로 재서명하는 환경에서 Windows에서 CA 번들을 export해
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`에 설정하는 경우가 있습니다.

**WSL(Linux)에서 이 경로는 유효하지 않습니다** (`C:\path\…` 형식).
`debug/langfuse_client.py`는 시작 시 Linux 환경에서 Windows 경로가 감지되면
해당 env var를 **번역하지 않고 unset**합니다 — 시스템 CA(`certifi`)로 폴백됩니다.

수동 확인:

```bash
python -c "import os; print(os.environ.get('REQUESTS_CA_BUNDLE'))"
# None이면 정상 (unset됨)
```

---

## 4. SDK-primary vs MCP-optional 설계 근거

| 항목 | SDK (`debug/langfuse_client.py`) | Community MCP (`langfuse-mcp`) |
|---|---|---|
| 수락 기준 의존 | **예** — 모든 필수 검증 | 아니오 |
| 버전 고정 | `langfuse==4.15.0` (requirements.txt) | uvx 최신 |
| WSL CA 처리 | 자동 unset | 미처리 |
| 오프라인/CI | 가능 (mock) | 불가 |
| 세션 간 지속성 | 코드로 버전 관리 | 세션 한정 |

MCP는 "트레이스 직접 보기" 인터랙티브 요청의 편의 레이어입니다.
모든 자동화·CI·수락 기준은 SDK 경로로만 동작합니다.
