#!/usr/bin/env bash
# run-backend.sh — 호환용 셈. 본체는 `scripts/stack.sh run backend` (2026-09-14 통합). 설치된 유닛 파일이
# 아직 이 이름을 가리키므로 한 릴리스 동안 남긴다 — 다음 PR 에서 scripts/systemd/* 의 ExecStart 를
# stack.sh 로 바꾸고 이 파일을 지운다 (배포 때 _common.sh units_refresh 가 유닛을 갱신한다).
exec "$(dirname -- "${BASH_SOURCE[0]}")/stack.sh" run backend "$@"
