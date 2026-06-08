# CAMCHAT Frontend (Next.js 16)

부산외국어대학교 학사 챗봇 **CAMCHAT** 의 프론트엔드. Next.js 16 App Router + React 19 + TypeScript + Tailwind v4 기반. 다국어 UI (ko/en), SSE 스트리밍, 관리자 콘솔, 성적표 업로드/분석을 포함한다.

## 요구사항

- **Node.js 20+** · npm / yarn / pnpm 택 1
- **백엔드** (`backend/main.py`, FastAPI) 가 `http://localhost:8000` 에서 실행 중이어야 `/api/*` 프록시가 작동한다. 프로덕션에서는 nginx(`docker/nginx/default.conf`) 가 리버스 프록시로 묶어 준다.
- 백엔드 구동 방법은 프로젝트 루트 `README.md` 참조 (`cd docker && docker compose up -d --build`).

## 환경 변수

`.env.local` 또는 `.env` 에 필요 시 설정. 기본값으로도 로컬 개발 가능.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `""` (상대 경로 `/api/*` 사용) | 백엔드 API 루트. 같은 도메인에서 서비스하면 비워둠. 로컬에서 백엔드 포트 분리 개발 시 `http://localhost:8000` 지정 가능 |
| `NEXT_PUBLIC_SITE_URL` | 자동 | 메타 태그 / OG 링크용 (선택) |

실제 프로덕션 설정은 nginx 가 같은 도메인에서 `/api/*` 를 backend 로 라우팅하므로 프론트엔드는 상대 경로만 사용하는 구조.

## 로컬 개발

백엔드 기동 상태에서 (또는 별도 포트에서 실행 중)

```bash
npm install
npm run dev
```

→ `http://localhost:3000` 에서 접속. API 호출은 `/api/*` 로 감 — nginx 없이 개발 시 `next.config.ts` 의 rewrites 또는 `NEXT_PUBLIC_API_BASE_URL` 활용.

프로덕션 빌드:

```bash
npm run build
npm run start
```

Dockerfile 기반 프로덕션은 `docker/Dockerfile.frontend` 가 multi-stage build → standalone 산출물 → nginx 뒤에서 serve.

## 디렉토리 구조

```
frontend/
├── src/
│   ├── app/
│   │   ├── layout.tsx              # RootLayout (Noto Sans KR, meta)
│   │   ├── page.tsx                # 루트 → /[lang]/chat 리다이렉트
│   │   ├── globals.css             # Tailwind v4 + 커스텀 스타일
│   │   ├── [lang]/                 # 다국어 동적 라우팅 (ko / en)
│   │   │   └── chat/page.tsx       # 메인 챗봇 페이지
│   │   └── admin/                  # 관리자 콘솔 (/admin)
│   ├── components/
│   │   ├── chat/                   # ChatView, MessageBubble, WelcomeScreen, RatingBar, ...
│   │   ├── layout/                 # Sidebar, Header, TranscriptUpload, AcademicReport, ...
│   │   ├── notifications/          # 사용자 알림 드롭다운
│   │   └── report/                 # 성적표 분석 결과 (ProgressGrid, SemesterChart, GradeDonut, GraduationTimeline, RetakeTable, ActionChecklist, NextTermGuide)
│   ├── hooks/                      # useSession, useChat(SSE), useTranscript 등
│   └── lib/
│       ├── api.ts                  # apiFetch() fetch 래퍼
│       ├── i18n.ts                 # t(lang, key) 번역 함수 + 메시지 맵
│       └── types.ts                # 공유 타입
├── public/                         # 정적 자원 (파비콘 등)
├── next.config.ts · tsconfig.json · eslint.config.mjs · postcss.config.mjs · package.json
```

## 주요 컴포넌트

| 컴포넌트 | 경로 | 역할 |
|---|---|---|
| `ChatView` | `components/chat/ChatView.tsx` | 메시지 스트림 뷰 — SSE 토큰 단위 렌더, 출처 카드, 별점 |
| `WelcomeScreen` | `components/chat/WelcomeScreen.tsx` | 랜딩 — 추천 질문 4-8개, 학사일정 안내 |
| `RatingBar` | `components/chat/RatingBar.tsx` | 답변 하단 1~5점 + 자유 피드백 입력 |
| `Sidebar` | `components/layout/Sidebar.tsx` | 좌측 네비 — 새 대화 / 학사리포트 / 공지 |
| `TranscriptUpload` | `components/layout/TranscriptUpload.tsx` | 성적표 업로드 (consent 체크 → POST `/api/transcript/upload`) |
| `AcademicReport` | `components/layout/AcademicReport.tsx` | 성적표 분석 대시보드 (GET `/api/transcript/analysis`) |
| `NotificationsDropdown` | `components/notifications/...` | FAQ 이송·답변 알림 — 로그인 사용자 한정 |
| `AdminConsole` | `app/admin/...` | 관리자 — FAQ 큐레이션, 크롤러 제어, 로그, 연락처 |

## i18n (다국어 ko/en)

- 경로 기반 라우팅: `/ko/chat`, `/en/chat`. 루트 `/` 접근 시 `Accept-Language` 헤더로 자동 리다이렉트.
- 번역 소스: `src/lib/i18n.ts` 의 `messages` 객체 — 키 기반. `t(lang, "report.title")` 형식으로 사용.
- 백엔드 EN 파이프라인은 FlashText (`config/academic_terms.yaml`) + BGE-M3 크로스링구얼 + M2M-100 번역으로 구성 — 프론트엔드는 단순히 `lang` 파라미터만 전달.

## 스타일링

- **Tailwind v4** + `postcss.config.mjs` — `src/app/globals.css` 에서 `@import "tailwindcss"`
- **Noto Sans KR** (Google Fonts, `next/font` 로 번들) — `layout.tsx` 에서 전역 지정
- 색상 토큰은 `globals.css` 의 CSS 변수로 중앙 관리 (`--primary`, `--muted` 등)
- 다크 모드 미사용 (학생 제출 문서 대조 명료성 우선)

## API 호출 규칙

- `apiFetch<T>(path, init)` — 공통 JSON fetch 래퍼 (에러 정규화 + `NEXT_PUBLIC_API_BASE_URL` prefix)
- SSE 스트리밍 (`/api/chat/stream`) 은 직접 `fetch` + reader — `useChat` 훅 참고
- 관리자 API 는 쿠키 기반 JWT (백엔드 `backend/routers/admin/auth.py`)

## 배포

프로덕션은 **Docker + nginx**:

1. `docker/Dockerfile.frontend` — Next.js standalone 빌드
2. `docker/docker-compose.yml` — frontend 컨테이너 (`:3000`) + nginx (`:80`) + backend (`:8000`)
3. `docker/nginx/default.conf` — `/ → frontend`, `/api/* → backend`, `/_next/static/` 1년 캐시, SSE proxy_buffering off

외부 노출은 Cloudflare Tunnel (`~/.cloudflared/config.yml`) 로 `maruvis.co.kr` 도메인에 매핑.

## 자주 겪는 이슈

| 증상 | 해결 |
|---|---|
| 로컬에서 `/api/*` 404 | 백엔드가 `:8000` 에 안 떠있거나 `NEXT_PUBLIC_API_BASE_URL` 미설정. `docker compose up -d backend` 또는 `.env.local` 에 `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` |
| 502 Bad Gateway (프로덕션) | nginx upstream cache stale. `docker compose restart nginx`. 상세는 루트 `README.md` 트러블슈팅 |
| CORS 에러 | 백엔드 `.env` 의 `CORS_ORIGINS` 에 프론트 URL 포함 확인 |
| 성적표 업로드 무반응 | 대부분 502 (nginx). `curl http://localhost:8000/api/transcript/status?session_id=...` 로 직접 점검 |

## 기타

- 전체 시스템 문서: 프로젝트 루트 `README.md`
- 운영 매뉴얼: `docs/OPS_내부팀.md` (개발자), `docs/OPS_학사지원팀.md` (학사지원팀 비개발자)
- 라이선스: 루트 `LICENSE` 참조
