#!/usr/bin/env bash
# deploy.sh — 운영 서버 스위치 하나. 릴리스 태그 배포, 롤백, 점검 모드, 지금 뭐가 떠 있는지.
# 빌드·유닛 재기동·헬스 확인은 전부 restart-all.sh 에 맡기고, 이 스크립트가 직접 프로세스를
# 띄우는 일은 없다.
#
#   ./scripts/deploy.sh v0.2.0-alpha          # 그 릴리스 태그를 배포 (바뀌는 커밋 보여주고 y/N)
#   ./scripts/deploy.sh rollback              # 직전 배포로 — 묻지 않음
#   ./scripts/deploy.sh rollback v0.1.0-alpha # 지정한 태그로
#   ./scripts/deploy.sh status                # 운영 중 태그 / 체크아웃 / 실제 도는 프로세스 / 헬스 / 점검 모드
#   ./scripts/deploy.sh tags                  # origin 의 릴리스 태그 최신순, 운영 중인 것 표시
#   ./scripts/deploy.sh maint on|off          # 점검 모드: Worker 503 안내 페이지 + healthcheck 자동 재기동 중지
#
# 플래그: --yes                y/N 확인 생략 (배포만)
#         --dry-run            검사만 하고 아무것도 바꾸지 않음
#         --no-auto-rollback   재기동 실패 시 이전 버전으로 되돌리지 않고 멈춤
#         --local-only         (maint) healthcheck 플래그만 건드리고 Cloudflare Worker 는 그대로
#
# 배포 순서:
#   1. 태그 fetch; 태그는 존재하고, vX.Y.Z[-접미사] 모양이고, origin/main 에 머지돼 있어야 함
#   2. 이 체크아웃이 systemd 가 서비스하는 그 폴더여야 하고, 커밋 안 된 수정이 없어야 함
#      (rollback 은 대신 stash 해 둠 — 비상시에 남의 수정 때문에 멈추면 안 되니까)
#   3. 새로 나가는(또는 빠지는) 커밋을 보여주고 확인
#   4. git checkout --detach <태그>
#   5. restart-all.sh — 필요하면 프론트 재빌드, stop, start, /health + /health/llm 확인
#   6. logs/run/deploys.log 에 한 줄 추가 — 마지막 "ok" 행이 곧 운영 중인 버전
# restart-all.sh 종료 코드와 그다음:
#   0  정상                                   → 운영 중으로 기록
#   3  프론트 빌드 실패, 서버는 안 건드림       → 체크아웃만 되돌림, 재기동 없음
#   4  백엔드는 응답, /health/llm 실패          → 운영 중(성능 저하)으로 기록, 롤백 없음, 큰 경고
#   1  백엔드 무응답                            → 직전 운영 태그로 자동 롤백 (--no-auto-rollback 이면 멈춤)
#
# 누가 태그를 발행하고 누가 배포하는지, 버전 이름 규칙: RELEASE.md.
#
# 테스트 훅 (healthcheck-cron.sh 와 같은 방식): DEPLOY_RESTART_CMD 가 restart-all.sh 를 대신,
# DEPLOY_SKIP_UNIT_CHECK=1 이면 "systemd 가 서비스하는 체크아웃인가" 검사를 건너뜀.

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

DEPLOY_LOG="$RUN_DIR/deploys.log"   # TSV, 시도마다 한 행: 시각  동작  태그  sha  결과  계정
TAG_RE="$RELEASE_TAG_RE"            # _common.sh — restart-all.sh 배너와 같은 패턴
# Worker 점검 페이지는 wrangler 로 켜고 끈다. 화면 없는 서버라 API 토큰이 필요하고,
# scripts/env.local 은 _common.sh 가 읽으니 거기 한 줄이면 아래 모든 npm/wrangler 호출에 전달된다.
WRANGLER_AUTH_HINT='scripts/env.local 에 export CLOUDFLARE_API_TOKEN=... 한 줄, 또는 cd worker && npx wrangler login'

yes=0; dry_run=0; auto_rollback=1; local_only=0
positional=()

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() { echo "[error]  $*" >&2; exit 1; }
say() { echo "$*"; }

g() { git -C "$REPO" "$@"; }

now() { date '+%Y-%m-%d %H:%M:%S'; }

# ---------------------------------------------------------------------------------------
# 상태 — deploys.log 하나가 기록 전부; "운영 중" = 결과가 "ok" 로 시작하는 마지막 행
# ---------------------------------------------------------------------------------------

# DEP_TAG / DEP_SHA / DEP_TIME 를 채운다 (배포 기록이 없으면 전부 빈 값).
read_deployed() {
    DEP_TAG=""; DEP_SHA=""; DEP_TIME=""
    [ -f "$DEPLOY_LOG" ] || return 0
    IFS=$'\t' read -r DEP_TAG DEP_SHA DEP_TIME < <(
        awk -F'\t' -v re="$TAG_RE" '$5 ~ /^ok/ && $3 ~ re { t = $3; s = $4; d = $1 } END { printf "%s\t%s\t%s\n", t, s, d }' "$DEPLOY_LOG"
    ) || true
}

record() {  # $1 동작  $2 태그  $3 sha  $4 결과
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(now)" "$1" "$2" "$3" "$4" "${USER:-?}" >>"$DEPLOY_LOG"
}

# 롤백 대상: 성공한 행을 거꾸로 훑으며, 운영 중이 아니고 그 뒤에 롤백으로 떠난 적도 없는
# 가장 최근 태그 — 그래서 `rollback` 두 번은 계속 뒤로 간다 (v0.3 → v0.2 → v0.1), 방금 버린
# 릴리스로 앞으로 가지 않는다. 명시적 `deploy` 는 그 표시를 지운다.
previous_tag() {
    [ -f "$DEPLOY_LOG" ] || return 0
    awk -F'\t' -v re="$TAG_RE" '
        $5 ~ /^ok/ && $3 ~ re {
            if (($2 == "rollback" || $2 == "auto-rollback") && live != "") abandoned[live] = 1
            if ($2 == "deploy") delete abandoned[$3]
            live = $3; tags[++n] = $3
        }
        END { for (i = n; i >= 1; i--) if (tags[i] != live && !(tags[i] in abandoned)) { print tags[i]; exit } }
    ' "$DEPLOY_LOG"
}

# origin 의 릴리스 태그, 최신순, 한 줄에 태그<TAB>날짜<TAB>제목.
# verify_tag 가 받아 줄 이름만 — "v1" 이나 "vfoo" 같은 태그는 릴리스가 아니다.
release_tags() {
    g "${GIT_VERSIONSORT[@]}" for-each-ref --sort=-v:refname \
        --format='%(refname:short)%09%(creatordate:short)%09%(contents:subject)' 'refs/tags/v*' \
        | awk -F'\t' -v re="$TAG_RE" '$1 ~ re'
}

latest_release_tag() { release_tags | head -1 | cut -f1; }

# ---------------------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------------------

# systemd 유닛은 자기가 돌릴 체크아웃 경로를 박아 둔다. 다른 clone 에서 배포하면 아무도
# 서비스하지 않는 파일을 바꾸고, 엉뚱한 것을 돌리는 유닛을 재기동하게 된다.
check_serving_repo() {
    [ "${DEPLOY_SKIP_UNIT_CHECK:-0}" = 1 ] && return 0
    units_installed || return 0
    local wd
    wd="$(systemctl --user show -p WorkingDirectory --value camchat-backend.service 2>/dev/null || true)"
    [ -z "$wd" ] || [ "$wd" = "$REPO" ] \
        || die "systemd 가 서비스하는 체크아웃은 $wd 인데 여기는 $REPO — 운영 체크아웃에서 실행하세요."
}

dirty_files() { g status --porcelain --untracked-files=no; }

check_clean() {
    local dirty
    dirty="$(dirty_files)"
    [ -z "$dirty" ] || die "커밋 안 된 수정이 있습니다 — 운영은 태그와 정확히 같아야 하니 먼저 커밋하거나 git stash 하세요:"$'\n'"$dirty"
}

# 롤백은 비상 경로: 남의 수정 때문에 거부하지 말고 치워 두고 진행한다.
stash_if_dirty() {
    [ -n "$(dirty_files)" ] || return 0
    local label
    label="deploy.sh rollback $(now): 커밋 안 된 수정 임시 보관"
    g stash push --quiet -m "$label"
    say "[stash]  커밋 안 된 수정을 치워 뒀습니다 — stash 이름 \"$label\"" >&2
    say "         복구: git stash pop" >&2
}

# 태그의 기준은 origin: 로컬에만 있는 태그는 지우고(아무도 발행 안 한 태그는 릴리스가 아니다),
# 옮겨진 태그는 따라가고(--force), origin 에서 지운 태그는 사라진다.
fetch_tags() {
    g fetch origin --prune --prune-tags --force --tags --quiet \
        || die "git fetch 실패 — 네트워크가 없거나 origin 에 닿지 않습니다."
}

# 릴리스 태그 검증: 이름 패턴, origin 에 발행됨, origin/main 에서 닿음. TAG_SHA 를 채운다.
# 이 체크아웃의 refs/tags 를 믿지 않고 origin 에 직접 묻는다 — 서버에서 `git tag` 만 친 태그는
# 배포되면 안 된다 (RELEASE.md: 두 사람이지 한 사람이 아니다).
verify_tag() {
    local tag="$1" remote
    [[ "$tag" =~ $TAG_RE ]] || die "'$tag' 은(는) 릴리스 태그 이름이 아닙니다 (vX.Y.Z-alpha / vX.Y.Z-beta / vX.Y.Z, RELEASE.md)."
    remote="$(g ls-remote --tags origin "refs/tags/$tag" "refs/tags/$tag^{}")" \
        || die "origin 에 태그 '$tag' 를 물어볼 수 없습니다 — 네트워크가 없거나 origin 에 닿지 않습니다."
    # annotated 태그는 두 줄로 나온다; ^{} 줄이 태그가 가리키는 커밋
    remote="$(awk '$2 ~ /\^\{\}$/ { peeled = $1 } $2 !~ /\^\{\}$/ { plain = $1 } END { print (peeled != "" ? peeled : plain) }' <<<"$remote")"
    [ -n "$remote" ] || die "태그 '$tag' 가 origin 에 없습니다 — GitHub 에서 릴리스를 발행했나요? (./scripts/deploy.sh tags)"
    g fetch origin --force --quiet "refs/tags/$tag:refs/tags/$tag" || die "origin 에서 태그 '$tag' 를 가져오지 못했습니다."
    TAG_SHA="$(g rev-parse -q --verify "refs/tags/$tag^{commit}")"
    [ "$TAG_SHA" = "$remote" ] || die "태그 '$tag' 가 origin 에선 ${remote:0:7}, 여기선 ${TAG_SHA:0:7} — fetch 뒤에도 달라 중단합니다."
    g merge-base --is-ancestor "$TAG_SHA" origin/main \
        || die "태그 '$tag' 가 main 에 머지돼 있지 않습니다 — 릴리스는 main 에서만 냅니다."
}

dry_run_note() {
    say "[dry-run] 실행했다면: git checkout --detach $1 && ./scripts/restart-all.sh  — 아무것도 바꾸지 않았습니다."
}

# ---------------------------------------------------------------------------------------
# 전환
# ---------------------------------------------------------------------------------------

run_restart() {
    if [ -n "${DEPLOY_RESTART_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅: 명령줄이라 일부러 단어 분리
        $DEPLOY_RESTART_CMD
    else
        "$REPO/scripts/restart-all.sh"
    fi
}

# 릴리스 체크리스트(RELEASE.md)가 요구하는 기록, 채워서 출력 — GitHub Release 에 붙여넣는다.
# 서버 계정은 공용이라 DEPLOY_BY (scripts/env.local) 로 사람 이름을 넣는다.
release_record() {  # $1 = 태그  $2 = sha  $3 = 직전 운영 태그 (없으면 "")
    say
    say "[record] GitHub Release 설명(Edit release)에 붙여넣으세요:"
    say "         Version: $1"
    say "         Commit: ${2:0:7}"
    say "         Released: $(date '+%Y-%m-%d')"
    say "         Approved by: (릴리스 발행자)"
    say "         Deployed by: ${DEPLOY_BY:-$USER}"
    say "         Previous version: ${3:-없음}"
    say "         Rollback target: ${3:-없음}"
}

# 배포 시도 전의 체크아웃으로 복귀 (브랜치였으면 브랜치로).
restore_checkout() {  # $1 = 브랜치 이름 또는 "", $2 = sha
    if [ -n "$1" ]; then g checkout --quiet "$1"; else g checkout --quiet --detach "$2"; fi
}

# 전환이 끝난 뒤: 수동 `maint on` 플래그가 있었으면 되돌리고, 아니면 플래그를 확실히 지운다.
restore_maint() {  # $1 = 보관해 둔 수동 플래그 내용, 또는 ""
    if [ -n "$1" ]; then printf '%s\n' "$1" >"$MAINT_FLAG"; else maint_off; fi
}

# switch_to <태그> <sha> <동작> <롤백허용>
# 태그를 체크아웃하고 재기동하고 기록한다. 백엔드가 죽었고 롤백허용=1 이면 직전 운영 태그를
# 한 번 복원한다 — 릴리스 기록이 아예 없던 첫 배포라면 원래 체크아웃(브랜치/커밋)을 복원해
# 재기동하되, 릴리스가 아니므로 운영 중으로 기록하지 않는다.
switch_to() {
    local tag="$1" sha="$2" action="$3" allow_rollback="$4"
    local pre_branch pre_sha prev_tag maint_saved="" rc=0
    pre_branch="$(g symbolic-ref --short -q HEAD || true)"
    pre_sha="$(g rev-parse HEAD)"
    read_deployed; prev_tag="$DEP_TAG"

    # 전환 내내 healthcheck 를 세워 둔다: 체크아웃 + 프론트 빌드 구간이 몇 분이라, 그 사이
    # 타이머가 재기동하면 반쯤 빌드된 .next 로 프론트를 띄운다. restart-all.sh 는 자기 구간에서
    # 플래그를 세우고 지우므로, 원래 있던 수동 `maint on` 은 끝나고 되돌린다.
    [ -f "$MAINT_FLAG" ] && maint_saved="$(cat "$MAINT_FLAG")"
    MAINT_SAVED="$maint_saved"
    # shellcheck disable=SC2064  # 지금 확장하는 게 의도: trap 이 지역 변수에 기대면 안 됨
    trap "restore_maint '$MAINT_SAVED'" EXIT   # 빌드 중 Ctrl-C 해도 플래그가 남으면 안 됨
    maint_on "deploy.sh $action $tag"

    say "[switch] git checkout --detach $tag  (${sha:0:7})"
    if ! g checkout --quiet --detach "$tag"; then
        record "$action" "$tag" "$sha" checkout-failed
        restore_maint "$maint_saved"
        say "[error]  git checkout $tag 실패 — 재기동하지 않았습니다. 워크트리는 git 이 남긴 상태 그대로 (git status)." >&2
        return 1
    fi

    say "[switch] restart-all.sh"
    run_restart || rc=$?
    restore_maint "$maint_saved"
    trap - EXIT

    case "$rc" in
        0)
            record "$action" "$tag" "$sha" ok
            say
            say "[done]   $tag (${sha:0:7}) 운영 중.   되돌리기: ./scripts/deploy.sh rollback"
            if [ "$action" = deploy ]; then release_record "$tag" "$sha" "$prev_tag"; fi
            return 0 ;;
        3)
            # 프론트 빌드 실패 — restart-all.sh 가 서버를 건드리지 않았다
            record "$action" "$tag" "$sha" build-failed
            restore_checkout "$pre_branch" "$pre_sha"
            say "[undo]   프론트 빌드 실패, 서버는 그대로 — 체크아웃을 ${pre_branch:-${pre_sha:0:7}} 로 되돌렸습니다." >&2
            return 3 ;;
        4)
            # 백엔드는 응답, LLM 확인만 실패 — 코드는 이미 운영 중이다. 다시 재기동해도 ollama 가
            # 고쳐지지 않고 다운타임만 늘어나니, 유지하고 크게 알린다.
            record "$action" "$tag" "$sha" "ok (llm probe failed)"
            say "[warn]   $tag (${sha:0:7}) 운영 중이지만 /health/llm 실패 — 롤백하지 않습니다." >&2
            say "         ollama 확인: ./scripts/healthcheck.sh / systemctl --user status camchat-ollama" >&2
            if [ "$action" = deploy ]; then release_record "$tag" "$sha" "$prev_tag"; fi
            return 4 ;;
        *)
            record "$action" "$tag" "$sha" "restart-failed(rc=$rc)"
            if [ "$allow_rollback" != 1 ]; then
                say "[error]  재기동 실패 (rc=$rc), 자동 롤백 꺼짐 — 서버가 내려가 있을 수 있습니다." >&2
                say "         고치거나: ./scripts/deploy.sh rollback" >&2
                return 1
            fi
            # 이번 시도 전에 운영 중이던 것으로 돌아간다 (로그는 성공했을 때만 "ok" 행을 얻는다).
            # 첫 릴리스 전이라 운영 중인 것이 없으면, 원래 체크아웃(브랜치/커밋)을 복원해 재기동하되
            # 릴리스라고 부르지 않는다.
            local rc2=0
            read_deployed
            if [ -n "$DEP_TAG" ]; then
                say "[undo]   재기동 실패 (rc=$rc) — $DEP_TAG 로 롤백합니다" >&2
                switch_to "$DEP_TAG" "$DEP_SHA" auto-rollback 0 || rc2=$?
                case "$rc2" in
                    0|4) say "[undo]   $DEP_TAG 로 롤백됨; $tag 는 배포되지 않았습니다. logs/backend/server.err 확인." >&2 ;;
                    *)   say "[error]  롤백도 실패 — 서버가 내려가 있습니다. systemctl --user status camchat.target" >&2 ;;
                esac
            else
                local where="${pre_branch:-${pre_sha:0:7}}"
                say "[undo]   재기동 실패 (rc=$rc) — 이전 릴리스 기록이 없음. 복원: $where" >&2
                maint_on "deploy.sh restore $where"
                restore_checkout "$pre_branch" "$pre_sha"
                run_restart || rc2=$?
                restore_maint "$maint_saved"
                record auto-rollback "$where" "$pre_sha" "restored(rc=$rc2)"
                case "$rc2" in
                    0|4) say "[undo]   $where 복원됨; $tag 는 배포되지 않았습니다. logs/backend/server.err 확인." >&2 ;;
                    *)   say "[error]  $where 복원도 실패 — 서버가 내려가 있습니다. systemctl --user status camchat.target" >&2 ;;
                esac
            fi
            return 1 ;;
    esac
}

# ---------------------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------------------

cmd_deploy() {  # $1 = 태그
    local tag="$1" from
    check_serving_repo
    fetch_tags
    verify_tag "$tag"
    check_clean
    read_deployed

    from="${DEP_SHA:-$(g rev-parse HEAD)}"
    say "[deploy] $tag (${TAG_SHA:0:7})   지금 운영 중: ${DEP_TAG:-기록 없음} (${from:0:7})"
    if [ "$TAG_SHA" = "$from" ]; then
        say "[deploy] 운영 중인 커밋과 같음 — 재기동만 합니다."
    else
        # `| head` 가 아니라 --max-count: head 가 먼저 닫히면 git 이 SIGPIPE 를 받고, pipefail +
        # errexit 아래서는 40커밋 넘는 릴리스마다 확인 전에 이 스크립트가 죽는다.
        if g merge-base --is-ancestor "$from" "$TAG_SHA"; then
            say "[deploy] 새로 나가는 커밋 (최신순, 최대 40개):"
            g log --oneline --no-decorate --max-count=40 "$from..$TAG_SHA" | sed 's/^/           /'
        else
            say "[deploy] 이 태그는 운영 중인 것보다 오래됨 — 빠지는 커밋 (최대 40개):"
            g log --oneline --no-decorate --max-count=40 "$TAG_SHA..$from" | sed 's/^/           /'
        fi
    fi

    if [ "$dry_run" = 1 ]; then dry_run_note "$tag"; return 0; fi
    if [ "$yes" != 1 ]; then
        [ -t 0 ] || die "터미널이 아닙니다 — 확인 없이 배포하려면 --yes 를 붙이세요."
        local answer
        read -r -p "$tag 를 운영에 배포할까요? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { say "취소했습니다."; return 0; }
    fi
    switch_to "$tag" "$TAG_SHA" deploy "$auto_rollback"
}

cmd_rollback() {  # $1 = 태그 또는 ""
    local tag="${1:-}"
    check_serving_repo
    fetch_tags
    if [ -z "$tag" ]; then
        tag="$(previous_tag)"
        [ -n "$tag" ] || die "$DEPLOY_LOG 에 되돌아갈 이전 배포 기록이 없습니다 — 태그를 지정하세요: ./scripts/deploy.sh rollback vX.Y.Z-alpha"
    fi
    verify_tag "$tag"
    read_deployed
    say "[rollback] $tag (${TAG_SHA:0:7})   대체 대상: ${DEP_TAG:-기록 없음}"
    if [ "$dry_run" = 1 ]; then dry_run_note "$tag"; return 0; fi
    stash_if_dirty
    switch_to "$tag" "$TAG_SHA" rollback 0
}

cmd_tags() {
    fetch_tags
    read_deployed
    local n=0 tag date subject mark
    while IFS=$'\t' read -r tag date subject; do
        n=$((n + 1))
        mark=""; [ "$tag" = "$DEP_TAG" ] && mark="   <- 운영중"
        printf '%-18s %s  %s%s\n' "$tag" "$date" "$subject" "$mark"
    done < <(release_tags)
    [ "$n" -gt 0 ] || say "릴리스 태그가 아직 없습니다 — GitHub Releases -> Draft a new release 로 발행하세요 (RELEASE.md)."
}

cmd_status() {
    read_deployed
    local head_sha head_ref
    head_sha="$(g rev-parse HEAD)"
    if [ -n "$DEP_TAG" ] && [ "$head_sha" = "$DEP_SHA" ]; then
        head_ref="$DEP_TAG"
    else
        head_ref="$(head_release_tag)"
        [ -n "$head_ref" ] || head_ref="$(g symbolic-ref --short -q HEAD || echo detached)"
    fi

    if [ -n "$DEP_TAG" ]; then
        say "[live]     $DEP_TAG (${DEP_SHA:0:7})  배포 $DEP_TIME"
    else
        say "[live]     기록 없음 — 아직 첫 배포 전 (./scripts/deploy.sh <태그>)"
    fi

    if [ -z "$DEP_SHA" ] || [ "$head_sha" = "$DEP_SHA" ]; then
        say "[checkout] $head_ref (${head_sha:0:7})"
    else
        say "[checkout] $head_ref (${head_sha:0:7})  != 운영중 — 재기동(healthcheck·systemd)되면 $DEP_TAG 가 아니라 이게 뜹니다"
    fi

    # 백엔드는 시작할 때마다 자기 커밋을 로그에 남긴다 (run-backend.sh) — 실제로 도는 코드.
    local run_line run_sha
    run_line="$(grep -a '\[backend\] starting on' "$LOG_DIR/backend/server.out" 2>/dev/null | tail -1 || true)"
    if [ -n "$run_line" ]; then
        run_sha="$(sed -n 's/.*(\([0-9a-f]\{7,\}\)).*/\1/p' <<<"$run_line")"
        if [ -n "$DEP_SHA" ] && [ -n "$run_sha" ] && [ "${DEP_SHA:0:${#run_sha}}" != "$run_sha" ]; then
            say "[running]  백엔드 시작 커밋 $run_sha  != 운영중 $DEP_TAG (${DEP_SHA:0:7})"
        else
            say "[running]  백엔드 시작 커밋 ${run_sha:-?}"
        fi
    fi

    if units_installed; then
        local u st since line=""
        for u in ollama backend frontend; do
            st="$(systemctl --user is-active "camchat-$u.service" 2>/dev/null || true)"
            since="$(systemctl --user show -p ActiveEnterTimestamp --value "camchat-$u.service" 2>/dev/null | cut -d' ' -f2-3)"
            line="$line  $u=$st${since:+ ($since 부터)}"
        done
        say "[units]  $line"
    fi

    if [ -x "$REPO/scripts/healthcheck.sh" ]; then
        "$REPO/scripts/healthcheck.sh" 2>&1 | sed 's/^/[health]   /' || true
    fi

    if [ -f "$MAINT_FLAG" ]; then
        say "[maint]    ON — $(cat "$MAINT_FLAG")  — healthcheck 자동 재기동 중지됨 (./scripts/deploy.sh maint off)"
    else
        say "[maint]    off"
    fi
    if [ -f "$REPO/worker/package.json" ]; then
        # wrangler 는 값을 stdout 에(빈 줄 하나 덤으로), 키가 없으면 "Value not found" 를 stderr 에,
        # 물어볼 수조차 없으면 인증 오류를 stderr 에 낸다.
        local kv err
        err="$(mktemp)"
        kv="$(cd "$REPO/worker" && timeout 25 npm run --silent maintenance:status 2>"$err" || true)"
        kv="${kv//[$'\n\r\t ']/}"
        if [ "$kv" = on ]; then
            say "[worker]   점검 안내 페이지 ON (방문자에게 503 안내)"
        elif [ -n "$kv" ] || grep -qi 'not found' "$err"; then
            say "[worker]   점검 안내 페이지 off"
        else
            say "[worker]   점검 안내 페이지: 알 수 없음 — 이 서버의 wrangler 가 Cloudflare 에 접근 못 함 ($WRANGLER_AUTH_HINT)"
        fi
        rm -f "$err"
    fi

    local newest
    newest="$(latest_release_tag)"
    if [ -n "$newest" ] && [ "$newest" != "$DEP_TAG" ]; then
        say "[tags]     origin 최신 태그: $newest (운영 중 아님)  -> ./scripts/deploy.sh $newest"
    fi

    if [ -f "$DEPLOY_LOG" ]; then
        say "[history]"
        tail -5 "$DEPLOY_LOG" | awk -F'\t' '{ printf "           %s  %-14s %-18s %s\n", $1, $2, $3, $5 }'
    fi
}

# Worker 점검 키를 켜고 끈다. wrangler 가 인증할 것이 없으면 미리 거부하고(안 그러면 대화형
# 로그인 프롬프트에서 멈춘다), 시간을 제한해 멈춘 npx/wrangler 가 터미널을 붙들지 못하게 한다.
worker_kv() {  # $1 = on|off
    if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && [ ! -f "$HOME/.config/.wrangler/config/default.toml" ]; then
        say "[worker]   이 서버에 Cloudflare 인증 정보가 없습니다 — $WRANGLER_AUTH_HINT" >&2
        return 1
    fi
    (cd "$REPO/worker" && timeout 60 npm run --silent "maintenance:$1")
}

cmd_maint() {  # $1 = on|off
    local mode="$1" rc=0
    case "$mode" in
        on)
            maint_on "deploy.sh maint on (manual)"
            say "[maint]    healthcheck 자동 재기동 중지 (플래그: $MAINT_FLAG)"
            if [ "$local_only" = 1 ]; then return 0; fi
            if worker_kv on; then
                say "[worker]   점검 안내 페이지 ON — 방문자에게 최대 60초 안에 503 안내"
            else
                rc=1
                say "[error]  Cloudflare Worker 전환 실패 — 서버 쪽 플래그는 세워졌습니다. wrangler 인증 뒤 'maint on' 을 다시 하거나 --local-only 를 쓰세요." >&2
            fi ;;
        off)
            maint_off   # 먼저: Cloudflare 쪽이 어찌 되든 healthcheck 는 다시 돌아야 한다
            say "[maint]    healthcheck 자동 재기동 재개"
            if [ "$local_only" = 1 ]; then return 0; fi
            if worker_kv off; then
                say "[worker]   점검 안내 페이지 off — 최대 60초 안에 정상 화면"
            else
                rc=1
                say "[error]  Cloudflare Worker 전환 실패 — 방문자에게 아직 점검 페이지가 보입니다. wrangler 인증을 고친 뒤: cd worker && npm run maintenance:off" >&2
            fi ;;
        *) die "사용법: ./scripts/deploy.sh maint on|off [--local-only]" ;;
    esac
    return "$rc"
}

main() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            --yes)              yes=1 ;;
            --dry-run)          dry_run=1 ;;
            --no-auto-rollback) auto_rollback=0 ;;
            --local-only)       local_only=1 ;;
            -h|--help)          usage; exit 0 ;;
            --*)                die "알 수 없는 플래그: $arg (./scripts/deploy.sh --help)" ;;
            *)                  positional+=("$arg") ;;
        esac
    done
    local cmd="${positional[0]:-help}"
    case "$cmd" in
        help)      usage; exit 0 ;;
        status)    cmd_status ;;
        tags)      cmd_tags ;;
        rollback)  cmd_rollback "${positional[1]:-}" ;;
        maint)     cmd_maint "${positional[1]:-}" ;;
        v*)        cmd_deploy "$cmd" ;;
        *)         echo "알 수 없는 명령: $cmd" >&2; usage >&2; exit 2 ;;
    esac
}

main "$@"
