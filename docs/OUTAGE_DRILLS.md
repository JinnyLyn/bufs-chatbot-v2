# 모의 장애 시험 기록

`reports/CamChat-장애대응.pdf` §19 의 시험을 실시할 때마다 아래 표에 한 줄씩 추가한다(배포 전 1회, 이후 월 1회).
절차는 `worker/README.md`(시험 4·5)와 `scripts/healthcheck.sh` 를 따른다.

| 일자 | 시험 | 장애 유형 | 감지 | 복구 | 감지·복구 소요 | 발견된 문제 | 후속 개선 |
|---|---|---|---|---|---|---|---|
| 2026-09-08 | 4 — 전체 서비스 중단 | `systemctl --user stop cloudflared` (터널 정지) | `maruvis.kr/ko/chat` → **503** + `Retry-After: 300` + 한국어 안내(`x-camchat-outage: origin-502`), `/` 도 503. `/api/session` 은 Cloudflare 530 그대로(Worker 미경유, 설계대로) | `systemctl --user start cloudflared` | 정지 2초 뒤 안내 표시, 시작 지시 3초 뒤 200 복귀 — 총 접속 불가 7초 | 없음 | 외부 모니터(시행계획 3번) 미구축이라 자동 감지·알림은 아직 없음 |
| 2026-09-08 | 1 — 백엔드 프로세스 종료 | `kill -9 <backend pid>` (유닛 전환 직후) | `OnFailure` 웹훅 알림(로그) 즉시. systemd `Restart=on-failure` 가 15초 뒤 재기동 | 자동 | 강제 종료 34초 뒤 `/health` 정상(재기동 15초 + 기동 19초). `NRestarts=1` | 전환 직후 2분 점검의 첫 실행이 "아직 뜨는 중"인 백엔드를 실패 1회로 세어 둠 → 아래 시험 3에서 2회째로 집계돼 백엔드까지 같이 재기동됨 | 유닛이 `activating` 이거나 켜진 지 300초 미만이면 실패로 세지 않도록 수정(healthcheck-cron.sh) |
| 2026-09-08 | 3 — 프론트엔드 종료 | `kill -9 <frontend pid>` | `OnFailure` 알림. 2분 점검이 7초 뒤 실행돼 DOWN 감지(위 이월분과 합쳐 2회) → 백엔드·프론트 재기동 | 자동 | 12초 뒤 `:3000` 정상, maruvis.kr 200 | 위와 동일(이월 집계) | 위와 동일 |
| 2026-09-08 | 2 — 백엔드 무응답 | `kill -STOP <backend pid>` (프로세스는 살아 있고 응답 없음) | 멈춘 뒤 40초(01:28:10) 첫 점검 실패 → 알림(로그). 2분 뒤(01:30:07) 2회째 → `systemctl --user restart camchat-backend camchat-frontend` | 2분 점검이 2회 연속 실패 후 `systemctl --user restart` | 멈춤 → 새 프로세스 정상 응답까지 3분 11초 (01:27:30 → 01:30:41). 점검 주기 2분 × 2 + TERM 무응답 프로세스 KILL(30초) + 기동 | 재기동 때 프론트가 SIGTERM 으로 종료돼 exit 143 → systemd 가 '실패'로 기록, `OnFailure` 알림이 불필요하게 1건 나감 | 서비스 유닛에 `SuccessExitStatus=143` 추가(적용 완료) |
| 2026-09-08 | 5 — 점검 모드 | KV `maintenance=on` (`npm run maintenance:on`) | `maruvis.kr/ko/chat` → **503** 점검 안내(`x-camchat-outage: maintenance`, `Retry-After: 300`), `/api/session` 은 200 유지(설계대로) | `npm run maintenance:off` | on 지시 35초 뒤 점검 화면, off 지시 50초 뒤 200 복귀 (KV 엣지 캐시 ≤60초) | `npm run deploy/maintenance:*` 가 `wrangler: not found` — 스크립트를 `npx wrangler` 로 수정(PR #291) | wrangler 4 의 kv 명령은 `--remote` 필수(스크립트에 포함) |

## 시험별 절차 요약

- **시험 4 (전체 중단)**: 서버에서 `systemctl --user stop cloudflared` → 다른 네트워크에서 `curl -sI -H 'Accept: text/html' https://maruvis.kr/ko/chat` 가 503 이고 본문에 "챗봇 점검 중입니다" 가 있는지 → `systemctl --user start cloudflared` → 200 복귀 확인. 접속 불가 시간은 10초 안팎.
- **시험 5 (점검 모드)**: `cd worker && npm run maintenance:on` → 최대 1~2분 뒤 503 점검 안내(API 는 200 유지) → `npm run maintenance:off` → 1~2분 뒤 200.
- **시험 1·3 (프로세스 종료)**: `systemctl --user show -p MainPID --value camchat-backend.service` 로 pid 확인 → `kill -9` → 15초 뒤 유닛이 다시 뜨는지(`systemctl --user status camchat-backend`, `/health`), `logs/alerts.log` 에 OnFailure 알림이 남는지.
- **시험 2 (무응답)**: `kill -STOP <pid>` → 2분 점검이 두 번 실패(최대 4분) 후 재기동 → `logs/healthcheck.log`·`logs/alerts.log` 확인. 끝나면 옛 pid 가 남아 있지 않은지 확인.
