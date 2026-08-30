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

## 2. zip에 **안 따라온** 것들 (이관 시 반드시 다시 만들어야 함)

`.gitignore` / 레포 밖에 있던 항목들이라 압축·scp에 포함되지 않았을 가능성이 높다.

| 항목 | 원래 위치 | 조치 |
|---|---|---|
| **cloudflared 자격증명 JSON** | `C:\ProgramData\Cloudflared\<uuid>.json` (**레포 밖**) | 새 터널을 이미 만들었다면 새 JSON을 쓰면 된다. 기존 터널을 그대로 쓰려면 `cloudflared tunnel login` 후 재발급 |
| **cloudflared 서비스 설정** | `C:\ProgramData\Cloudflared\config.yml` (**레포 밖**) | `scripts/cloudflared-config.example.yml` 참고 → `~/.cloudflared/config.yml` |
| `project/.env` | 레포 안, gitignore | 아래 3절 템플릿으로 새로 작성 |
| `frontend/.env.local` | 레포 안, gitignore | `NEXT_PUBLIC_API_URL` — 터널 뒤 same-origin이면 **비워두면 된다**(`next.config.ts` rewrite가 `/api/*`를 `:8000`으로 넘김) |
| `win-ca-bundle.pem` | 레포 안, gitignore | **불필요.** Norton이 HTTPS를 가로채던 Windows 전용 우회였다. Linux에서는 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`을 **설정하지 말 것** |
| `frontend/node_modules`, `.next` | gitignore | `npm ci && npm run build` 로 재생성 |
| `backups/` | gitignore | 로컬 GPU 폴백 `.env` 백업본. 없어도 무방 |

`qdrant_db/`, `parent_store/`, `markdown_docs/`는 **커밋되어 있으므로** 그대로 따라왔다.
재인덱싱 없이 바로 뜬다. (`qdrant_db/.lock`만 gitignore — 남아 있으면 지운다.)

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
CORS_ORIGINS=http://localhost:3000,https://maruvis.co.kr

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

```bash
mkdir -p ~/.cloudflared
cp scripts/cloudflared-config.example.yml ~/.cloudflared/config.yml
# UUID / credentials-file 경로를 새 터널 값으로 수정

cloudflared tunnel list                       # UUID 확인
cloudflared tunnel route dns <uuid> maruvis.co.kr
cloudflared tunnel --config ~/.cloudflared/config.yml run <uuid>
```

> DNS CNAME이 아직 **옛 PC의 터널**을 가리키고 있으면 트래픽이 그쪽으로 간다.
> `tunnel route dns`로 새 터널에 다시 붙이거나, Cloudflare 대시보드에서 `maruvis.co.kr`
> CNAME을 `<새-uuid>.cfargotunnel.com`으로 바꾼다. 옛 PC의 cloudflared 서비스는
> 반드시 **정지**시킨다 (`Stop-Service cloudflared` + `Set-Service cloudflared -StartupType Disabled`),
> 안 그러면 두 터널이 같은 호스트명을 두고 경합한다.

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
- [ ] `kb_docs`가 0이 아닌가 (0이면 `qdrant_db/`가 안 따라온 것 → `python project/reindex.py`)
- [ ] `[llm/gpu ] ... gpu=100%` — H100에 완전히 올라갔는가 (CPU 스필 없음)
- [ ] `[frontend] ok :3000`
- [ ] `curl -I https://maruvis.co.kr` → 200, 그리고 브라우저에서 실제 질문 1건 응답
- [ ] 옛 PC의 cloudflared 서비스 정지 확인

`logs/backend/app.log`에서 `PIPELINE_TIMING`을 보면 단계별 지연을 확인할 수 있다.
H100이라면 기존 PC(4070, 26% GPU offload)보다 눈에 띄게 빨라야 정상이다.
