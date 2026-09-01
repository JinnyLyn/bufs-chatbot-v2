#!/usr/bin/env bash
# doc_sync.sh — pdfs/ 를 제어면으로 KB 문서를 추가/은퇴/복원하는 운영 스크립트.
#
# 개념:
#   pdfs/            현 시점 프로덕션 챗봇이 사용할 문서의 원본 (.pdf / .md)
#   pdfs/archive/    은퇴시킬(색인에서 뺄) 문서의 원본 또는 마커 파일
#   markdown_docs/   색인의 실제 소스 (문서 1개 = .md 1개) — 이 스크립트가 결과를 반영하는 곳
#   markdown_docs/archive/   은퇴된 .md 보관소 (색인 경로가 하위 폴더를 읽지 않아 자동 제외)
#
# 사용법:
#   scripts/doc_sync.sh [status]              # (기본) 페어링과 실행 계획만 출력, 아무것도 안 바꿈
#   scripts/doc_sync.sh apply [--restart]     # 은퇴/복원 이동 + 변경 있으면 reindex
#   scripts/doc_sync.sh add [--restart] <원본파일>...   # 새 문서를 ingest.py 로 변환+증분 색인
#   --restart = systemd 유닛(agentic-rag)을 stop → 작업 → start 로 감싼다.
#
# 운영 규칙 (성능 보호 하드 가드):
#   - 이미 존재하는 markdown_docs/*.md 는 절대 재변환/수정하지 않는다 (변환기 버전 차이로
#     청킹이 달라지는 것을 방지). add 는 매칭되는 md 가 없는 파일만 색인한다.
#   - 원본 파일명과 md 파일명은 공백/언더스코어/'+' 차이가 흔하다 (예:
#     "2026학년도_1학기_학사안내.pdf" ↔ "2026학년도1학기학사안내.md"). 매칭은 이 문자들을
#     제거한 정규화 stem 의 (접두 포함) 유일 매치로 판단하고, 애매하면 중단한다 —
#     잘못 매칭해 같은 문서를 이중 색인하면 검색 품질이 떨어진다.
#   - 원본이 pdfs/ 에 없는 md 전용 문서(과거 일괄 인제스트분)는 건드리지 않는다.
#     은퇴시키려면 같은 이름의 빈 마커 파일을 pdfs/archive/ 에 만들면 된다:
#       touch "pdfs/archive/수강신청 FAQ.pdf"
#   - 색인 작업(reindex/ingest)은 임베디드 Qdrant 락 때문에 백엔드가 내려간 상태에서만
#     가능하다. --restart 없이 백엔드가 떠 있으면 파일 이동도 하지 않고 중단한다.
#
# 테스트 오버라이드(운영에서는 설정하지 말 것):
#   DOC_SYNC_ROOT / DOC_SYNC_REINDEX_CMD / DOC_SYNC_INGEST_CMD / DOC_SYNC_SKIP_PORT_CHECK
set -euo pipefail

ROOT="${DOC_SYNC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PDFS="$ROOT/pdfs"
PDFS_ARCHIVE="$PDFS/archive"
MD="$ROOT/markdown_docs"
MD_ARCHIVE="$MD/archive"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
PORT="${PORT:-8000}"

mkdir -p "$PDFS" "$PDFS_ARCHIVE" "$MD" "$MD_ARCHIVE"

usage() { sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

die() { echo "ERROR: $*" >&2; exit 1; }

# 정규화 stem: 확장자 제거 후 공백/언더스코어/'+' 제거 (레포의 기존 파일명 인코딩 편차 흡수)
norm() { local b; b="$(basename "$1")"; b="${b%.*}"; printf '%s' "$b" | tr -d ' _+'; }

# $1(정규화 stem)와 유일하게 매칭되는 $2 디렉터리 안의 .md 경로를 출력.
# 매치 0개면 빈 출력. 2개 이상이면 die — 호출부는 반드시 `m="$(match_md ...)"` 형태의
# 단독 할당으로 부를 것 (if 조건 안에서 부르면 set -e 가 die 를 삼킨다).
match_md() {
    local key="$1" dir="$2" hits=() f n
    for f in "$dir"/*.md; do
        [ -e "$f" ] || continue
        n="$(norm "$f")"
        if [ "$n" = "$key" ] || [[ "$n" == "$key"* ]] || [[ "$key" == "$n"* ]]; then
            hits+=("$f")
        fi
    done
    [ "${#hits[@]}" -le 1 ] || die "'$key' 매칭이 애매합니다 (${hits[*]}) — 파일명을 정리한 뒤 다시 실행하세요."
    [ "${#hits[@]}" -eq 0 ] || printf '%s' "${hits[0]}"
}

backend_up() {
    [ -n "${DOC_SYNC_SKIP_PORT_CHECK:-}" ] && return 1
    command -v lsof >/dev/null 2>&1 && lsof -ti ":$PORT" >/dev/null 2>&1
}

require_backend_down_or_restart() {  # $1 = restart 플래그("1"|"")
    if backend_up && [ -z "$1" ]; then
        die "백엔드가 :$PORT 에 떠 있습니다 (임베디드 Qdrant 락). --restart 를 붙이거나 먼저 내리세요: systemctl --user stop agentic-rag"
    fi
}

svc_stop()  { echo ">> systemctl --user stop agentic-rag";  systemctl --user stop agentic-rag; }
svc_start() { echo ">> systemctl --user start agentic-rag"; systemctl --user start agentic-rag; }

run_reindex() {
    if [ -n "${DOC_SYNC_REINDEX_CMD:-}" ]; then eval "$DOC_SYNC_REINDEX_CMD"; return; fi
    "$PY" "$ROOT/project/reindex.py"
}

run_ingest() {  # $@ = 원본 파일들 — 기존 ingest.py 경로 재사용 (변환 + 증분 색인)
    if [ -n "${DOC_SYNC_INGEST_CMD:-}" ]; then eval "$DOC_SYNC_INGEST_CMD \"\$@\""; return; fi
    "$PY" "$ROOT/project/ingest.py" "$@"
}

# ── 계획 수립 ────────────────────────────────────────────────────────
declare -A ACTIVE_KEYS=()
for f in "$PDFS"/*.pdf "$PDFS"/*.md; do
    [ -e "$f" ] || continue
    ACTIVE_KEYS["$(norm "$f")"]="$f"
done
RETIRE=() RESTORE=() UNMATCHED_ACTIVE=() STALE_MARKERS=()
for f in "$PDFS_ARCHIVE"/*; do
    [ -e "$f" ] || continue
    key="$(norm "$f")"
    [ -z "${ACTIVE_KEYS[$key]:-}" ] || die "'$(basename "$f")' 가 pdfs/ 와 pdfs/archive/ 양쪽에 있습니다 — 한쪽만 남기세요."
    m="$(match_md "$key" "$MD")"
    if [ -n "$m" ]; then
        RETIRE+=("$m")
    else
        m="$(match_md "$key" "$MD_ARCHIVE")"
        # 이미 은퇴 완료된 마커는 정상 상태(현상 유지 기록)라 침묵; 아무것과도 안 맞으면 오타 경고.
        [ -n "$m" ] || STALE_MARKERS+=("$f")
    fi
done
for key in "${!ACTIVE_KEYS[@]}"; do
    m="$(match_md "$key" "$MD")"
    if [ -z "$m" ]; then
        m="$(match_md "$key" "$MD_ARCHIVE")"
        if [ -n "$m" ]; then RESTORE+=("$m"); else UNMATCHED_ACTIVE+=("${ACTIVE_KEYS[$key]}"); fi
    fi
done

print_status() {
    local n_active n_archived
    n_active=$(find "$MD" -maxdepth 1 -name '*.md' | wc -l)
    n_archived=$(find "$MD_ARCHIVE" -maxdepth 1 -name '*.md' | wc -l)
    echo "KB 활성 문서(markdown_docs/*.md): $n_active  |  은퇴(archive/): $n_archived"
    echo
    echo "[은퇴 예정 — pdfs/archive/ 기준] ${#RETIRE[@]}건"
    for m in "${RETIRE[@]+"${RETIRE[@]}"}"; do echo "  - $(basename "$m") → markdown_docs/archive/"; done
    echo "[복원 예정 — pdfs/ 로 되돌아온 원본] ${#RESTORE[@]}건"
    for m in "${RESTORE[@]+"${RESTORE[@]}"}"; do echo "  - archive/$(basename "$m") → markdown_docs/"; done
    echo "[md 없음 — 'doc_sync.sh add <파일>' 로 추가 필요] ${#UNMATCHED_ACTIVE[@]}건"
    for f in "${UNMATCHED_ACTIVE[@]+"${UNMATCHED_ACTIVE[@]}"}"; do echo "  - $(basename "$f")"; done
    if [ "${#STALE_MARKERS[@]}" -gt 0 ]; then
        echo "[경고 — 어떤 md 와도 매칭 안 되는 pdfs/archive/ 항목 (오타?)] ${#STALE_MARKERS[@]}건"
        for f in "${STALE_MARKERS[@]}"; do echo "  - $(basename "$f")"; done
    fi
}

cmd="${1:-status}"
case "$cmd" in
    -h|--help) usage; exit 0 ;;

    status)
        [ $# -le 1 ] || die "status 는 추가 인자를 받지 않습니다."
        print_status
        if [ "${#RETIRE[@]}" -gt 0 ] || [ "${#RESTORE[@]}" -gt 0 ]; then
            echo; echo "적용하려면: scripts/doc_sync.sh apply [--restart]"
        fi
        ;;

    apply)
        shift
        restart=""
        for a in "$@"; do
            case "$a" in
                --restart) restart=1 ;;
                *) die "알 수 없는 인자: $a (허용: --restart)" ;;
            esac
        done
        print_status
        if [ "${#RETIRE[@]}" -eq 0 ] && [ "${#RESTORE[@]}" -eq 0 ]; then
            echo; echo "변경 없음 — 이동/재색인 생략."; exit 0
        fi
        require_backend_down_or_restart "$restart"
        [ -n "$restart" ] && svc_stop
        for m in "${RETIRE[@]+"${RETIRE[@]}"}";  do mv "$m" "$MD_ARCHIVE/"; done
        for m in "${RESTORE[@]+"${RESTORE[@]}"}"; do mv "$m" "$MD/"; done
        echo; echo ">> reindex (markdown_docs/ 기준 클린 재빌드)"
        run_reindex
        [ -n "$restart" ] && svc_start
        echo
        echo "완료. markdown_docs/ 의 이동을 커밋하세요 (git 이 rename 으로 감지합니다)."
        ;;

    add)
        shift
        restart=""
        srcs=()
        for a in "$@"; do
            case "$a" in
                --restart) restart=1 ;;
                -*) die "알 수 없는 인자: $a (허용: --restart)" ;;
                *) srcs+=("$a") ;;
            esac
        done
        [ "${#srcs[@]}" -ge 1 ] || die "add 는 원본 파일 경로가 필요합니다."
        todo=()
        for src in "${srcs[@]}"; do
            [ -f "$src" ] || die "파일 없음: $src"
            case "$src" in *.pdf|*.md) ;; *) die "지원하지 않는 형식: $src (.pdf / .md 만)" ;; esac
            key="$(norm "$src")"
            m="$(match_md "$key" "$MD")"
            if [ -n "$m" ]; then
                echo "  (skip — 이미 존재: $(basename "$m")) $(basename "$src")"; continue
            fi
            m="$(match_md "$key" "$MD_ARCHIVE")"
            [ -z "$m" ] || die "'$(basename "$src")' 는 은퇴 문서($(basename "$m"))와 매칭됩니다 — 복원하려면 원본을 pdfs/ 로 옮기고 apply 하세요."
            todo+=("$src")
        done
        if [ "${#todo[@]}" -eq 0 ]; then echo "추가할 새 문서 없음."; exit 0; fi
        require_backend_down_or_restart "$restart"
        [ -n "$restart" ] && svc_stop
        echo ">> ingest (변환 + 증분 색인): ${#todo[@]}건"
        run_ingest "${todo[@]}"
        for src in "${todo[@]}"; do
            case "$src" in "$PDFS"/*) ;; *) cp -n "$src" "$PDFS/" 2>/dev/null || true ;; esac
        done
        [ -n "$restart" ] && svc_start
        echo
        echo "완료. 새로 생긴 markdown_docs/*.md 를 커밋하세요."
        ;;

    *) die "알 수 없는 명령: $cmd (status | apply | add | --help)" ;;
esac
