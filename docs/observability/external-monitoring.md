# 외부 상태 감시 (UptimeRobot)

서버 안의 healthcheck 타이머(`scripts/healthcheck-cron.sh`, 2분)와 별개로, **학생이 실제로 지나는 경로**
(인터넷 → Cloudflare → Tunnel → 서버)를 밖에서 5분마다 찍는다. 근거: `reports/CamChat-장애대응.pdf` §5.

## 등록할 모니터 (UptimeRobot 무료, 5분 간격)

| 모니터 | 종류 | URL | 정상 | 무엇을 잡나 |
|---|---|---|---|---|
| CamChat 페이지 | HTTP(s) | `https://maruvis.kr/` | 200 | 학생이 챗봇 화면에 들어갈 수 있는가. 서버 전체 장애 때는 Worker 가 **503** 을 내므로 장애로 잡힌다 |
| CamChat API | HTTP(s) | `https://maruvis.kr/api/health` | 200, 본문 `{"status":"ok"}` | Cloudflare → Tunnel → 백엔드 경로. 백엔드가 죽거나 뜨는 중이면 5xx/타임아웃 |

- 알림 연락처: 운영 담당자 이메일 + 앱 푸시(UptimeRobot 앱). 필요하면 같은 Discord/Slack 웹훅(`ALERT_WEBHOOK_URL`)도 연결.
- 키워드 모니터를 쓸 경우 페이지 쪽은 `대학 학사정보 전용 안내 챗봇`(랜딩 문구), API 쪽은 `"status":"ok"`.
- `/api/health` 는 **백엔드 프로세스가 살아 있으면** 200 이다 — ollama 가 멈춰 답변이 안 나오는 상황은 외부 모니터에 안 잡힌다. 그 부류는 서버 안의 healthcheck 타이머(`/health/llm` 검사 + 웹훅 알림)가 맡는다. 그래서 `ALERT_WEBHOOK_URL` 이 실제로 설정돼 있어야 한다.
- `/api/health` 는 내부 정보를 노출하지 않는다. 모델·GPU·경로가 보이는 `/health`, `/health/llm` 은 터널 밖으로 나가지 않는다(`/api/` 만 백엔드로 라우팅).

## 한계

- 외부 모니터는 **인터넷 → Cloudflare → Tunnel → 서버** 만 본다. 교내망에서만 나는 DNS·방화벽·차단 문제는 못 잡는다(§5). 교내 한정 장애가 반복되면 교내 PC 에서 도는 별도 확인을 검토.
- Worker 점검 모드(`npm run maintenance:on`)도 503 이라 모니터에 장애로 뜬다 — 계획 점검 전에는 모니터를 일시 정지(pause)한다.

## 등록 절차 (1회, 담당자)

1. uptimerobot.com 가입 → Add New Monitor → HTTP(s) → 위 URL 두 개, Interval 5 min.
2. Alert Contacts 에 담당자 이메일·앱 푸시 추가, 각 모니터에 연결.
3. 등록 직후 두 모니터가 Up 인지 확인. 서버에서 `bash scripts/healthcheck.sh` 가 ALL OK 인 상태여야 한다.
4. 모의 시험(보고서 §19 시험 4)을 한 번 더 돌려 "Down → Up" 알림이 실제로 오는지 본다. 결과는 `docs/OUTAGE_DRILLS.md` 에 기록.
