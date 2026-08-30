# Windows PC → H100 공유 서버 이관 (웹 서빙)

이 문서는 **기존 Windows PC(`C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main`)에서
웹이 어떻게 서빙되고 있었는지**를 복원하고, 그것을 H100 공유 서버로 옮기는 절차를 정리한다.

Docker는 어디에도 쓰이지 않았다. **cloudflared가 Windows 서비스로 등록되어** 있었고,
그 뒤에 로컬 프로세스 3개(Next.js / FastAPI / Ollama)가 떠 있는 구조였다.

---

## 1. 기존 PC의 서빙 구조

```
 인터넷 ──▶ https://maruvis.co.kr
              │
              ▼
   cloudflared  (Windows 서비스, named tunnel a20ee14c-cb8c-4de1-ae03-db7cb096e558)
   설정: C:\ProgramData\Cloudflared\config.yml
              │
     ┌────────┴─────────────────────────────┐
     │ path ^/api/                          │ 그 외 전부
     ▼                                      ▼
 FastAPI :8000                         Next.js :3000
 (python project/server.py)            (frontend/, npm run dev)
     │
     ▼
 Ollama  ── 로컬 RTX 4070 :11435  또는  원격 H100 :11434 (SSH 터널)
```

구성 요소별 근거는 전부 레포 안에 커밋되어 있다:

| 요소 | 파일 | 내용 |
|---|---|---|
| 터널 ingress | `logs/cf-config-new.yml` | `maruvis.co.kr/api/*` → `:8000`, 나머지 → `:3000` |
| 터널 적용 스크립트 | `scripts/apply-maruvis-tunnel.ps1` | 위 yml을 `C:\ProgramData\Cloudflared\config.yml`로 복사 후 `Restart-Service cloudflared` |
| 스택 기동 | `scripts/start-all.ps1` | Ollama(:11435) + backend(:8000) + frontend(:3000) |
| 스택 종료 | `scripts/stop-all.ps1` | 위 3개 종료, SSH 터널(:11434)은 건드리지 않음 |
| 자동 시작 | `scripts/register-autostart.ps1` | 작업 스케줄러 태스크 `AgenticRAG-Stack`, 로그온 시 `start-all.ps1` |
| 상태 점검 | `scripts/healthcheck.ps1` | `/health`, `/health/llm`, :3000 |

**마지막 운영 상태**(`logs/backend/server.err`)는 로컬 4070이 아니라 **원격 H100을 쓰고 있었다**:

```
runtime={'model': 'qwen3.5:9b', 'ollama_base_url': 'http://127.0.0.1:11434',
         'num_ctx': 16384, 'embedding_device': 'cpu', 'langfuse_enabled': True, 'kb_docs': 23}
Uvicorn running on http://0.0.0.0:8000
```

즉 PC는 **프론트+백엔드+터널만** 돌리고, 추론은 SSH 터널(`ssh -N -L 11434:localhost:11434`)로
H100에 넘기고 있었다. **이번 이관의 핵심은: 그 터널이 없어진다는 것.** H100 위에서는
Ollama가 같은 머신의 `127.0.0.1:11434`에 그냥 있다.

### 프론트엔드 서빙 방식 주의

`frontend/next.config.ts`에 `output: "standalone"`이 있어서 **`next start`는 동작하지 않는다.**
실제 로그에도 남아 있다:

```
⚠ "next start" does not work with "output: standalone" configuration.
  Use "node .next/standalone/server.js" instead.
```

기존 `start-all.ps1`은 이걸 피해 `npm run dev`(개발 서버)로 운영하고 있었다.
H100에서는 아래 `start-all.sh`가 standalone 빌드가 있으면 그걸 쓰고, 없으면 dev로 떨어진다.

---

## 2. zip으로 **따라온** 것들 — 그대로 쓰면 안 되고 **교체**해야 함

디렉토리 전체를 zip → scp 했으므로 gitignore된 파일들도 **전부 따라왔다.**
문제는 그 안에 (1) **Windows 전용 설정**과 (2) **이전 관리자 계정의 자격증명**이
들어 있다는 것. 관리자가 떠났으므로 아래는 재사용이 아니라 **교체 대상**이다.

| 항목 | 상태 | 조치 |
|---|---|---|
| `project/.env` | 따라옴 — 이전 관리자의 Langfuse 키 + Windows 전용 줄 포함 | **새로 작성** (3-2절 템플릿). 특히 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` 줄은 반드시 삭제(리눅스에서 그 경로가 없거나, 있으면 TLS를 되레 깨뜨림). `LANGFUSE_*` 키는 새 팀 프로젝트 키로 |
| `backups/` (`.env.bak-*`) | 따라옴 — **옛 키의 사본들** | 새 `.env` 확정 후 **삭제**. 옛 키가 여기 남아 돌아다니는 걸 막는다 |
| `win-ca-bundle.pem` | 따라옴 | **삭제.** Norton AV의 HTTPS 재서명 우회용 Windows 전용 산물 |
| `frontend/.env.local` | 따라옴 (`NEXT_PUBLIC_API_URL=http://localhost:8000`) | 터널 뒤 same-origin이면 **비워도 된다**(`next.config.ts` rewrite가 `/api/*`를 `:8000`으로 넘김). 남겨도 무방하나 절대 옛 도메인/호스트를 넣지 말 것 |
| `frontend/node_modules`, `.next` | 따라옴 — **Windows용 네이티브 바이너리** | 그대로 실행하면 깨진다. `rm -rf node_modules .next && npm ci && npm run build` |
| `.venv`/`venv` (있다면) | Windows용 | 삭제 후 리눅스에서 재생성 |
| `qdrant_db/.lock` | 따라왔을 수 있음 | 남아 있으면 삭제 (단일 프로세스 락) |
| cloudflared 자격증명·설정 | **안 따라옴** (`C:\ProgramData\` — 레포 밖) | 이미 해결됨: 새 Cloudflare 프로필 + 새 도메인 **maruvis.kr** + 새 터널 |

`qdrant_db/`, `parent_store/`, `markdown_docs/`는 커밋된 데이터라 그대로 쓴다 —
재인덱싱 불필요.

### 2-1. 관리자 교체에 따른 계정/자격증명 인수인계 체크리스트

레포·인프라에서 **이전 관리자 개인 계정에 묶여 있는 것들**. 코드 밖(각 서비스
콘솔)에서 처리해야 하는 항목이 대부분이다.

- [ ] **Langfuse** — `project/.env`의 `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`는 이전
      관리자의 프로젝트 키. 새 팀 조직/프로젝트를 만들고 새 키 발급 → `.env` 교체.
      (기존 트레이스·평가 데이터는 옛 프로젝트에 남는다 — 필요하면 떠나기 전에
      멤버 초대로 접근권을 넘겨받을 것.) `.mcp.json`은 env를 읽으므로 수정 불필요.
- [ ] **GitHub Actions** (`Settings → Secrets and variables → Actions`) —
      `.github/workflows/tests.yml`이 쓰는 값들이 이전 관리자 인프라를 가리킨다:
  - [ ] secret `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` → 새 키로 교체
  - [ ] var `OLLAMA_BASE_URL` → 옛 관리자 박스(테일넷)일 가능성. H100을 CI에서
        못 부르면 비워서 라이브 LLM 테스트를 스킵
  - [ ] secret `CLAUDE_CODE_OAUTH_TOKEN` (`claude.yml`) → 이전 관리자의 Claude
        계정 토큰. 새 관리자 계정으로 재발급
- [ ] **Tailscale** — 옛 PC(`100.99.54.5`)와 jin의 4090(`100.91.6.58`,
      `eval_tools/kpi/` 참고)은 이전 팀의 테일넷 노드. 이관 후 H100 추론은
      로컬이므로 운영에는 불필요 — 옛 PC를 테일넷에서 제거.
- [ ] **Cloudflare (옛 계정)** — `maruvis.co.kr` 존/터널(`a20ee14c-…`)은 이전
      관리자 계정 소유. 새 배포(maruvis.kr, 새 프로필)와 무관하지만, 옛 PC의
      cloudflared 서비스가 아직 돌고 있으면 **옛 도메인으로 옛 배포가 계속
      서빙된다** — 옛 PC에서 서비스 정지 + 가능하면 옛 계정에서 터널/DNS 정리.
- [ ] **옛 PC 잔재** — 작업 스케줄러 태스크 `AgenticRAG-Stack` 해제
      (`Unregister-ScheduledTask -TaskName 'AgenticRAG-Stack'`), cloudflared 서비스
      정지·비활성 (`Stop-Service cloudflared; Set-Service cloudflared -StartupType Disabled`).
- [ ] **HF Hub** — 로그상 비인증으로 쓰고 있었음(`HF_TOKEN` 없음). 교체할 것 없음.
      레이트리밋이 걸리면 새 계정 토큰을 `.env`에 추가.

> `scripts/apply-maruvis-tunnel.ps1`·`logs/cf-config-new.yml`은 옛 Windows/maruvis.co.kr
> 배포용 기록물이다. 새 배포에서는 `scripts/cloudflared-config.example.yml`을 쓴다.

---

## 3. H100에서의 셋업

### 3-1. 의존성

```bash
cd ~/agentic-rag-for-dummies-main   # scp로 푼 위치
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # --cert win-ca-bundle.pem 붙이지 말 것 (Windows 전용이었음)

cd frontend && npm ci && npm run build && cd ..
```

Node는 `frontend/.nvmrc` 기준(22 이상). Python은 3.12.

### 3-2. `project/.env` (H100 프로파일)

기존 PC 값에서 **바뀌는 부분만** 표시했다. 나머지는 `project/.env.example` 주석 참조.

```dotenv
# 터널 없음 — Ollama가 같은 머신에 있다
OLLAMA_BASE_URL=http://127.0.0.1:11434

LLM_MODEL=qwen3.5:9b
LLM_REASONING=false
STRUCTURED_OUTPUT_METHOD=function_calling

# H100 80GB 프로파일 (.env.example 권장값 — 12GB 4070용 8192/2000이 아님)
LLM_NUM_CTX=16384
BASE_TOKEN_THRESHOLD=12000

LLM_NUM_PREDICT=2048
LLM_REPEAT_PENALTY=1.1
LLM_KEEP_ALIVE=-1
LLM_WARMUP=true

EMBEDDING_DEVICE=cpu          # 임베딩은 CPU, VRAM은 LLM에 양보 (공유 서버라 더 중요)
SEARCH_SCORE_THRESHOLD=0.3

# 터널이 same-origin으로 넘기므로 CORS는 사실상 불필요하지만, 직접 접근용으로 남겨둠
CORS_ORIGINS=http://localhost:3000,https://maruvis.kr

# ⚠ 새 팀 Langfuse 프로젝트의 키 (이전 관리자 키 재사용 금지 — 2-1절)
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# SSL_CERT_FILE / REQUESTS_CA_BUNDLE 은 설정하지 않는다 (Norton 우회용 Windows 전용)
```

모델이 H100에 없으면: `ollama pull qwen3.5:9b`

### 3-3. 공유 서버 포트 충돌

`:3000` / `:8000` / `:11434`는 공유 박스에서 남이 이미 쓰고 있을 수 있다.
스크립트는 전부 환경변수로 덮어쓸 수 있게 해 두었다:

```bash
BACKEND_PORT=8010 FRONTEND_PORT=3010 ./scripts/start-all.sh
```

포트를 바꿨다면 **`~/.cloudflared/config.yml`의 ingress도 같이 바꿔야 한다.**

### 3-4. 기동 / 종료 / 점검

```bash
chmod +x scripts/*.sh          # 최초 1회

./scripts/start-all.sh         # Ollama + backend + frontend
./scripts/healthcheck.sh       # /health, /health/llm(GPU offload%), :3000
./scripts/stop-all.sh          # frontend + backend 종료 (Ollama는 유지)
./scripts/stop-all.sh --with-ollama
```

`start-all.sh`는 이미 떠 있는 포트는 건드리지 않고, 시작한 PID만 `logs/run/*.pid`에
기록한다. `stop-all.sh`는 **그 PID만** 종료한다 — 공유 서버에서 포트로 kill 하면
남의 프로세스를 잡을 수 있어서 일부러 그렇게 하지 않았다.

### 3-5. cloudflared

새 배포는 **새 Cloudflare 프로필 + 새 도메인 `maruvis.kr` + 새 터널**이다
(옛 `maruvis.co.kr`은 이전 관리자 계정 — 2-1절 참조). 터널을 이미 만들었다면
ingress만 맞추면 된다:

```bash
mkdir -p ~/.cloudflared
cp scripts/cloudflared-config.example.yml ~/.cloudflared/config.yml
# UUID / credentials-file 경로를 새 터널 값으로 수정

cloudflared tunnel list                       # UUID 확인
cloudflared tunnel route dns <uuid> maruvis.kr   # 이미 라우팅했다면 생략
cloudflared tunnel --config ~/.cloudflared/config.yml run <uuid>
```

> 도메인이 다르므로 옛 터널과 경합하진 않지만, 옛 PC의 cloudflared 서비스가 살아
> 있으면 `maruvis.co.kr`로 옛 배포가 계속 노출된다 — 2-1절대로 정지시킬 것.

### 3-6. 자동 시작 (`register-autostart.ps1` 대체)

**root/sudo가 있으면** systemd 시스템 유닛, **없으면**(공유 서버에서 흔함) systemd `--user`:

```bash
loginctl enable-linger "$USER"     # 로그아웃 후에도 유지
mkdir -p ~/.config/systemd/user
```

`~/.config/systemd/user/agentic-rag.service`:

```ini
[Unit]
Description=Agentic RAG stack (Ollama + FastAPI + Next.js)
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%h/agentic-rag-for-dummies-main
ExecStart=%h/agentic-rag-for-dummies-main/scripts/start-all.sh
ExecStop=%h/agentic-rag-for-dummies-main/scripts/stop-all.sh
TimeoutStartSec=600

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agentic-rag.service
```

systemd를 쓸 수 없으면 `tmux new -d -s rag './scripts/start-all.sh'`로도 충분하다.

---

## 4. 이관 검증 체크리스트

```bash
./scripts/healthcheck.sh
```

- [ ] `[backend ] ok  ... ollama=http://127.0.0.1:11434` — **터널이 아니라 로컬 Ollama**를 보고 있는가
- [ ] `kb_docs`가 0이 아닌가 (0이면 `qdrant_db/`가 손상된 것 → `python project/reindex.py`)
- [ ] `[llm/gpu ] ... gpu=100%` — H100에 완전히 올라갔는가 (CPU 스필 없음)
- [ ] `[frontend] ok :3000` — Windows에서 온 `node_modules`/`.next`를 지우고 리눅스에서 재빌드했는가
- [ ] `curl -I https://maruvis.kr` → 200, 그리고 브라우저에서 실제 질문 1건 응답
- [ ] Langfuse **새 프로젝트** 대시보드에 방금 질문의 트레이스가 찍히는가 (옛 프로젝트로 가면 옛 키가 남은 것)
- [ ] `grep -rn 'SSL_CERT_FILE\|REQUESTS_CA_BUNDLE' project/.env` → 없음 / `backups/`·`win-ca-bundle.pem` 삭제됨
- [ ] 옛 PC의 cloudflared 서비스·`AgenticRAG-Stack` 태스크 정지 확인 (2-1절)

`logs/backend/app.log`에서 `PIPELINE_TIMING`을 보면 단계별 지연을 확인할 수 있다.
H100이라면 기존 PC(4070, 26% GPU offload)보다 눈에 띄게 빨라야 정상이다.
