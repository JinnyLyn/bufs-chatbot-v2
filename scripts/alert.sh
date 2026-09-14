#!/usr/bin/env bash
# alert.sh — 호환용 셈. 본체는 `scripts/healthcheck.sh alert <제목> <본문>` (2026-09-14 통합). 설치된
# camchat-alert@.service 가 아직 이 이름을 가리키므로 한 릴리스 동안 남긴다 — 다음 PR 에서 ExecStart 를
# 바꾸고 이 파일을 지운다.
exec "$(dirname -- "${BASH_SOURCE[0]}")/healthcheck.sh" alert "$@"
