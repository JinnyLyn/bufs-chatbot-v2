# 모의 장애 시험 기록

`reports/CamChat-장애대응.pdf` §19 의 시험을 실시할 때마다 아래 표에 한 줄씩 추가한다(배포 전 1회, 이후 월 1회).
절차는 `worker/README.md`(시험 4·5)와 `scripts/healthcheck.sh` 를 따른다.

| 일자 | 시험 | 장애 유형 | 감지 | 복구 | 감지·복구 소요 | 발견된 문제 | 후속 개선 |
|---|---|---|---|---|---|---|---|
| 2026-09-08 | 4 — 전체 서비스 중단 | `systemctl --user stop cloudflared` (터널 정지) | `maruvis.kr/ko/chat` → **503** + `Retry-After: 300` + 한국어 안내(`x-camchat-outage: origin-502`), `/` 도 503. `/api/session` 은 Cloudflare 530 그대로(Worker 미경유, 설계대로) | `systemctl --user start cloudflared` | 정지 2초 뒤 안내 표시, 시작 지시 3초 뒤 200 복귀 — 총 접속 불가 7초 | 없음 | 외부 모니터(시행계획 3번) 미구축이라 자동 감지·알림은 아직 없음 |
| 2026-09-08 | 5 — 점검 모드 | KV `maintenance=on` (`npm run maintenance:on`) | `maruvis.kr/ko/chat` → **503** 점검 안내(`x-camchat-outage: maintenance`, `Retry-After: 300`), `/api/session` 은 200 유지(설계대로) | `npm run maintenance:off` | on 지시 35초 뒤 점검 화면, off 지시 50초 뒤 200 복귀 (KV 엣지 캐시 ≤60초) | `npm run deploy/maintenance:*` 가 `wrangler: not found` — 스크립트를 `npx wrangler` 로 수정(PR #291) | wrangler 4 의 kv 명령은 `--remote` 필수(스크립트에 포함) |

## 시험별 절차 요약

- **시험 4 (전체 중단)**: 서버에서 `systemctl --user stop cloudflared` → 다른 네트워크에서 `curl -sI -H 'Accept: text/html' https://maruvis.kr/ko/chat` 가 503 이고 본문에 "챗봇 점검 중입니다" 가 있는지 → `systemctl --user start cloudflared` → 200 복귀 확인. 접속 불가 시간은 10초 안팎.
- **시험 5 (점검 모드)**: `cd worker && npm run maintenance:on` → 최대 1~2분 뒤 503 점검 안내(API 는 200 유지) → `npm run maintenance:off` → 1~2분 뒤 200.
- 시험 1~3(백엔드 종료·무응답, 프론트 종료)은 시행계획 1~2번(systemd Restart, healthcheck 타이머) 구현 뒤에 실시한다.
