#!/usr/bin/env bash
# restart-all.sh — 호환용 셈. 본체는 `scripts/stack.sh restart` (2026-09-14 통합). 지금 운영 중인 이전
# 릴리스의 deploy.sh 가 새 태그를 체크아웃한 뒤 이 이름을 부른다 (deploy.sh 는 떠나는 태그의 것이 돈다) —
# 통합된 첫 릴리스가 배포되고 나면 다음 PR 에서 지운다.
exec "$(dirname -- "${BASH_SOURCE[0]}")/stack.sh" restart "$@"
