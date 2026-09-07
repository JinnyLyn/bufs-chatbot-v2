# worker/ — maruvis.kr 장애 안내 Worker

Cloudflare Worker 하나가 `maruvis.kr/*` 앞단에 서서, **서버(터널·프론트엔드) 자체에 접속할 수
없을 때** 학생에게 Cloudflare 기본 영문 오류 화면 대신 한국어 안내 페이지를 보여 준다.
근거: `reports/CamChat-장애대응.pdf` §11–§12 (현재 요금제에서 Custom Errors 사용 불가). PDF 는 PR #289 와
함께 들어오며, 그 전에는 초안 `reports/REPORT_장애대응.md` 의 "상황 2" 절을 본다.

## 동작

| 요청 | 정상 | 원본 서버 응답 불가(fetch 실패 또는 502/503/504/52x/530) |
|---|---|---|
| 페이지 이동(GET/HEAD, `Sec-Fetch-Dest: document` 또는 `Accept: text/html`) | 그대로 전달 | **HTTP 503 + `Retry-After: 300` + 안내 HTML** |
| `/api/*`, `/_next/*`, 정적 파일, AI 답변 스트림 | 운영 라우트에 포함되지 않아 Worker 를 거치지 않음 (preview 에서는 그대로 전달) | 프론트엔드가 자체 안내를 띄운다 |

- 안내 페이지: "챗봇 점검 중입니다 / 현재 일시적으로 서비스에 접속할 수 없습니다…" + 학사지원팀
  전화 + 학사일정표·홈페이지 링크 + 부서별 내선. 5분마다 자동 재접속(`meta refresh`).
- 200 이 아니라 503 으로 내려서 외부 모니터(UptimeRobot 등)는 여전히 장애로 인식한다.
- **점검 모드**: KV `maintenance=on` 이면 서버가 멀쩡해도 페이지 요청에 점검 안내를 보낸다.
  API 는 계속 통과한다(운영자 점검 중 상태 확인용). 재배포 없이 켜고 끈다. KV 값은 엣지에
  최대 60초 캐시되므로 **켜고 끈 뒤 반영까지 1분쯤** 걸린다(즉시 확인하면 이전 상태가 보인다).
  wrangler 4 의 `kv key put/get/delete` 는 기본이 **로컬** 저장소라 반드시 `--remote` 를 붙인다
  (npm 스크립트에는 들어 있다).
- 안내 페이지의 부서 내선은 `test/contacts-sync.test.mjs` 가 프론트 `CONTACT_GROUPS`(PR #289)와 대조한다 —
  한쪽만 고치면 `npm test` 가 실패한다.
- Worker 도 Cloudflare 위에서 돌므로 **Cloudflare 자체 장애나 교내망에서 maruvis.kr 차단**은
  막지 못한다(보고서 §12). 그 경우는 학교 공지 채널.

## 파일

- `wrangler.toml` — 이름·계정·KV 바인딩·라우트. 라우트는 `[env.production]` 에만 있고, 운영 스크립트는
  `workers_dev = false` 라 maruvis.kr 라우트로만 들어온다(workers.dev 우회 입구 없음).
- `src/index.js` — 판별·전달·503 응답. `handle(request, env, originFetch)` 로 테스트 주입 가능.
- `src/pages.js` — 안내 HTML(인라인 CSS, 외부 자원 없음). **연락처·학사일정 링크는 `frontend/src/lib/constants.ts`
  (`CONTACT_GROUPS`, `EMERGENCY_CONTACTS`, `PORTAL_LINKS` — PR #289 에서 추가)와 수동 동기화** — 바꿀 때 둘 다.
  Worker 는 빌드 단계가 없어 프론트 모듈을 import 할 수 없다.
- `test/worker.test.mjs` — node 내장 러너. `npm test`.

## 준비 (1회)

인증은 브라우저 로그인 대신 API 토큰(템플릿 "Edit Cloudflare Workers", Zone = maruvis.kr).
서버에 `~/.config/cloudflare/env` 로 두고 매번 `source` 한다. Git 에 넣지 않는다.

```bash
source ~/.config/cloudflare/env
cd worker
npx wrangler whoami                              # 계정 확인
npx wrangler kv namespace create OUTAGE          # 운영용  → [env.production] 의 id
npx wrangler kv namespace create OUTAGE_PREVIEW  # preview 용 → 최상위 [[kv_namespaces]] 의 id
```

preview 와 운영은 **다른** 네임스페이스를 쓴다. preview 에서 점검 모드를 연습해도 운영 학생
화면에는 절대 영향이 없다. (이미 생성돼 `wrangler.toml` 에 id 가 들어 있다.)

## 배포 순서 — 운영 앞에 두기 전에 preview 로 검증

```bash
source ~/.config/cloudflare/env && cd worker
npm test                                 # 1) 단위 테스트
npx wrangler deploy --dry-run --env ""   # 2) 설정·번들 검사 (--env "" = 최상위 preview 환경)
npx wrangler deploy --env ""             # 3) workers.dev preview 로 배포 (라우트 없음, 운영 무영향)
```

preview 확인(URL 은 3) 출력에 나온다):

```bash
P=https://camchat-outage.<subdomain>.workers.dev
curl -sI -H 'Accept: text/html' "$P/ko/chat" | head -1        # 정상: HTTP/2 200 (원본 통과)
curl -s -X POST -H 'Content-Type: application/json' -d '{"lang":"ko"}' "$P/api/session"   # 정상: 세션 JSON
# (/api/health 는 시행계획 3번에서 추가되기 전까지 백엔드 404 JSON 이 그대로 통과한다 — Worker 는 정상)
# 장애 페이지 미리보기: preview 만 죽은 원본으로 향하게 한 뒤
npx wrangler deploy --env "" --var ORIGIN_HOST:origin-down.invalid
curl -s -H 'Accept: text/html' "$P/" | grep -o '챗봇 점검 중입니다'   # 503 + 안내 페이지
npx wrangler deploy --env ""                                  # 되돌리기
# 점검 모드
npx wrangler kv key put --binding OUTAGE --env "" --remote maintenance on
sleep 70                                                       # KV 엣지 캐시(60초)
curl -sI -H 'Accept: text/html' "$P/" | grep -E 'HTTP|Retry-After'  # 503, Retry-After: 300
npx wrangler kv key delete --binding OUTAGE --env "" --remote maintenance
```

운영 라우트 바인딩(이 순간부터 maruvis.kr 트래픽이 Worker 를 거친다):

```bash
npm run deploy            # = wrangler deploy --env production  (routes = maruvis.kr/*)
curl -sI https://maruvis.kr/ko/chat | head -1     # 200 이어야 한다
curl -s -X POST -H 'Content-Type: application/json' -d '{"lang":"ko"}' https://maruvis.kr/api/session   # 백엔드 JSON 그대로
```

## 운영

```bash
source ~/.config/cloudflare/env && cd worker
npm run maintenance:on       # 점검 안내 켜기 (API 는 통과)
npm run maintenance:status
npm run maintenance:off
npm run tail                 # 실시간 로그 (X-CamChat-Outage 헤더로 사유 확인)
```

- 모의 장애 시험(보고서 §19 시험 4): 서버에서 `systemctl --user stop cloudflared` 후
  `curl -sI https://maruvis.kr/ | head -1` 이 503 이고 본문에 안내가 있는지, 외부 모니터가
  장애로 잡는지 확인. 끝나면 `systemctl --user start cloudflared`.
- **긴급 해제**(Worker 자체가 문제일 때): `npx wrangler delete --env production` 으로 Worker 와
  라우트를 제거하면 트래픽이 다시 터널로 직행한다. 또는 대시보드 Workers Routes 에서 라우트만 삭제.
- 이전 버전으로 되돌리기: `npx wrangler rollback --env production`.
- **재배포 시점**: Worker 를 새로 배포하면 진행 중이던 요청은 30초 유예 뒤 끊긴다(Cloudflare 동작).
  긴 답변 스트림(부하 시 p95 78초)이 잘릴 수 있으니 운영 재배포는 이용이 적은 시간에 한다.
- **Fail open**: Worker 코드가 예외를 던지면 요청을 그대로 원본으로 보낸다(`index.js` 기본 export). 무료 요금제
  일일 요청 한도(10만) 초과 시에도 사이트가 막히지 않도록, 대시보드 Workers Routes 의 해당 라우트에서
  **"Fail open"** 을 켜 둔다(기본은 fail closed = Error 1027). 라우트 바인딩 직후 1회 확인.
- **라우트 범위**: 페이지 URL(`/`, `/*/chat`, `/*/chat/`)만 Worker 를 거친다. `/api/*`·`/_next/*`·정적 파일은
  Worker 없이 터널로 직행하므로 답변 스트림은 재배포 유예의 영향을 받지 않는다. 프론트에 페이지 라우트가
  늘면 `wrangler.toml` 의 routes 도 같이 늘린다.
