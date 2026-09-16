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
#   ./scripts/deploy.sh deps                  # 현재 체크아웃의 의존성을 강제로 설치 (npm ci + pip, torch 는 고정)
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
#   5. 의존성이 바뀌었으면 설치: package-lock.json → npm ci, requirements.txt → pip (공유 .venv,
#      torch 는 지금 버전으로 고정). 설치 실패는 빌드 실패처럼 취급 — 서버는 안 건드리고 체크아웃 복원.
#   6. restart-all.sh — 필요하면 프론트 재빌드, stop, start, /health + /health/llm 확인
#   7. logs/run/deploys.log 에 한 줄 추가 — 마지막 "ok" 행이 곧 운영 중인 버전
#   8. GitHub 릴리스 설명의 "배포 기록" 줄을 gh 로 채운다 (Version/Commit/Released/Deployed by/
#      Previous/Rollback). 태그는 안 건드리니 태그 규칙에 안 걸림. 실패해도 배포 결과엔 영향 없음.
# restart-all.sh 종료 코드와 그다음:
#   0  정상                                   → 운영 중으로 기록
#   3  프론트 빌드 실패, 서버는 안 건드림       → 체크아웃만 되돌림, 재기동 없음
#   4  백엔드는 응답, /health/llm 실패          → 운영 중(성능 저하)으로 기록, 롤백 없음, 큰 경고
#   1  백엔드 무응답                            → 직전 운영 태그로 자동 롤백 (--no-auto-rollback 이면 멈춤)
#
# 누가 태그를 발행하고 누가 배포하는지, 버전 이름 규칙: RELEASE.md.
#
# 테스트 훅 (healthcheck-cron.sh 와 같은 방식): DEPLOY_RESTART_CMD 가 restart-all.sh 를,
# DEPLOY_HEALTH_CMD 가 /health 확인을, DEPLOY_NPM_CMD / DEPLOY_PIP_CMD 가 의존성 설치를,
# DEPLOY_GH_CMD 가 gh 를 대신. DEPLOY_SKIP_UNIT_CHECK=1 이면 "systemd 가 서비스하는
# 체크아웃인가" 검사를 건너뜀 (CAMCHAT_UNITS_DIR 로 유닛 경로를 흉내낼 수도 있음 — _common.sh).

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

# 이 태그 직전에 운영 중이던 "다른" 태그 (같은 태그를 다시 배포해도 Previous 가 자기 자신이 되지 않게).
previous_of() {  # $1 = 태그
    [ -f "$DEPLOY_LOG" ] || return 0
    awk -F'\t' -v re="$TAG_RE" -v me="$1" '$5 ~ /^ok/ && $3 ~ re && $3 != me { t = $3 } END { print t }' "$DEPLOY_LOG"
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

# systemd 유닛은 자기가 돌릴 체크아웃 경로를 박아 둔다. 다른 폴더(개발·staging)에서 배포하면
# 아무도 서비스하지 않는 파일을 바꾸고 엉뚱한 스택을 띄우면서 "운영 중" 이라고 기록하게 된다.
# 유닛이 아예 없는 서버(CI, 노트북)에선 검사할 게 없다.
check_serving_repo() {
    [ "${DEPLOY_SKIP_UNIT_CHECK:-0}" = 1 ] && return 0
    local wd
    wd="$(units_serving_dir)"
    [ -z "$wd" ] || [ "$wd" = "$REPO" ] \
        || die "systemd 가 서비스하는 체크아웃은 $wd 인데 여기는 $REPO — 운영 폴더에서 실행하세요: cd $wd"
}

# 백엔드가 이 체크아웃의 포트에서 응답하나 (같은 커밋을 다시 배포할 때 재기동을 건너뛰어도 되는지).
backend_alive() {
    if [ -n "${DEPLOY_HEALTH_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        $DEPLOY_HEALTH_CMD
    else
        curl -fsS --max-time 5 "http://127.0.0.1:${BACKEND_PORT:-8000}/health" >/dev/null 2>&1
    fi
}

# 지금 도는 백엔드의 커밋 (run-backend.sh 가 시작 때 로그에 남김), 모르면 빈 값.
running_sha() {
    grep -a '\[backend\] starting on' "$LOG_DIR/backend/server.out" 2>/dev/null | tail -1 \
        | sed -n 's/.*(\([0-9a-f]\{7,\}\)).*/\1/p' || true
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

# 태그의 기준은 origin: 옮겨진 태그는 따라가고(--force), origin 에서 지운 태그는 사라진다.
# 로컬에만 있는 태그를 지우는 --prune-tags 는 운영 폴더에서만 — 개발 폴더의 wip 태그를 status 가
# 조용히 지우면 안 된다 (운영 폴더엔 아무도 로컬 태그를 만들지 않는다).
# --soft: 8초 제한, 못 닿으면 경고만 하고 마지막 fetch 기준으로 계속 (status 용).
fetch_tags() {
    local prune=() soft=0
    [ "${1:-}" = --soft ] && soft=1
    units_installed && prune=(--prune-tags)
    if [ "$soft" = 1 ]; then
        timeout 8 git -C "$REPO" fetch origin --prune "${prune[@]}" --force --tags --quiet 2>/dev/null \
            || say "[tags]     origin 에 못 닿음 — 태그 목록은 마지막 fetch 기준"
    else
        g fetch origin --prune "${prune[@]}" --force --tags --quiet \
            || die "git fetch 실패 — 네트워크가 없거나 origin 에 닿지 않습니다."
    fi
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

# 의존성 설치 — 태그가 말하는 버전으로 돌게 한다.
#   pip:  매번 `pip install -r requirements.txt` (이미 맞으면 몇 초 만에 no-op). 선언 diff 가 아니라
#         설치 상태를 기준으로 삼아야 공유 .venv 에 누가 손으로 뭘 깔았어도 배포 때 선언대로 돌아온다.
#         torch 계열은 지금 설치된 버전에 고정 — PyPI 기본 torch 는 CUDA 13 휠이라 이 서버에서 못 돈다
#         (MIGRATION_H100.md). 고정할 torch 를 못 찾으면 설치하지 않고 실패시킨다.
#   npm:  느리므로(1분) package-lock.json 이 $1..$2 사이에 바뀌었거나, node_modules 가 없거나 lock 보다
#         오래됐거나(지난 npm ci 가 중간에 죽은 경우), $1 = all 일 때만 `npm ci`.
# 실패하면 1.
install_deps() {  # $1 = 이전 sha 또는 all   $2 = 지금 sha
    local need_npm=0 lock="$REPO/frontend/package-lock.json" marker="$REPO/frontend/node_modules/.package-lock.json"
    if [ -f "$lock" ]; then
        if [ "$1" = all ]; then need_npm=1
        elif [ ! -f "$marker" ] || [ "$lock" -nt "$marker" ]; then need_npm=1
        elif ! g diff --quiet "$1" "$2" -- frontend/package-lock.json 2>/dev/null; then need_npm=1
        fi
    fi
    if [ "$need_npm" = 1 ]; then
        say "[deps]   npm ci (package-lock 변경 또는 node_modules 오래됨, 로그: logs/frontend/npm-ci.log)"
        if [ -n "${DEPLOY_NPM_CMD:-}" ]; then
            # shellcheck disable=SC2086  # 테스트 훅
            $DEPLOY_NPM_CMD || return 1
        else
            (cd "$REPO/frontend" && npm ci --no-audit --no-fund >"$LOG_DIR/frontend/npm-ci.log" 2>&1) \
                || { say "[error]  npm ci 실패 — $LOG_DIR/frontend/npm-ci.log" >&2; return 1; }
        fi
    fi
    if [ -f "$REPO/requirements.txt" ]; then
        say "[deps]   pip install -r requirements.txt (torch 계열은 현재 버전 고정, 로그: logs/backend/pip-install.log)"
        if [ -n "${DEPLOY_PIP_CMD:-}" ]; then
            # shellcheck disable=SC2086  # 테스트 훅
            $DEPLOY_PIP_CMD || return 1
        else
            local py="$REPO/.venv/bin/python" cons="$RUN_DIR/pip-constraints.txt"
            [ -x "$py" ] || { say "[error]  $REPO/.venv/bin/python 이 없습니다 — venv 확인" >&2; return 1; }
            "$py" -m pip freeze 2>/dev/null | grep -iE '^(torch|torchvision|torchaudio)==' >"$cons" || true
            if ! grep -q '^torch==' "$cons"; then
                say "[error]  .venv 에서 torch 를 못 찾았습니다 — 고정할 버전이 없어 설치를 멈춥니다 (MIGRATION_H100.md torch cu126)." >&2
                return 1
            fi
            "$py" -m pip install -q -r "$REPO/requirements.txt" -c "$cons" >"$LOG_DIR/backend/pip-install.log" 2>&1 \
                || { say "[error]  pip install 실패 — $LOG_DIR/backend/pip-install.log" >&2; return 1; }
        fi
    fi
    return 0
}

# 배포한 사람: scripts/env.local 의 DEPLOY_BY 가 있으면 그것, 없으면 gh 에 로그인된 GitHub 계정
# (서버 계정 team_b 는 공용이라 사람을 가리키지 않는다), 그것도 없으면 $USER.
deployer() {
    if [ -n "${DEPLOY_BY:-}" ]; then echo "$DEPLOY_BY"; return 0; fi
    local login
    login="$(gh_cmd api user --jq .login 2>/dev/null || true)"
    echo "${login:-${USER:-?}}"
}

gh_cmd() {
    if [ -n "${DEPLOY_GH_CMD:-}" ]; then
        # shellcheck disable=SC2086  # 테스트 훅
        $DEPLOY_GH_CMD "$@"
    else
        (cd "$REPO" && gh "$@")
    fi
}

# GitHub 릴리스 설명의 "배포 기록" 줄들을 채운다. 없으면 절을 덧붙인다. 태그 자체는 안 건드리므로
# 태그 규칙(성원만)에 안 걸린다. gh 가 없거나 실패하면 경고만 — 사람이 [record] 블록을 붙여 넣으면 된다.
update_release_record() {  # $1 = 태그  $2 = sha  $3 = 직전 운영 태그 (없으면 "")
    if [ -z "${DEPLOY_GH_CMD:-}" ] && ! command -v gh >/dev/null 2>&1; then
        say "[record] gh 가 없어 GitHub 릴리스 설명은 손으로 붙여 넣으세요."; return 0
    fi
    local json tmp meta rid url
    json="$(gh_cmd api "repos/{owner}/{repo}/releases/tags/$1" 2>/dev/null)" \
        || { say "[record] GitHub 릴리스($1)를 못 읽었습니다 — 위 블록을 손으로 붙여 넣으세요." >&2; return 0; }
    tmp="$(mktemp)"
    printf '%s' "$json" >"$tmp.in"
    # 한 번의 python: 있는 줄은 바꾸고, 없는 줄만 "## 배포 기록" 절에 덧붙인다. "Approved by" 는 성원이
    # 체크리스트에 이미 썼으면 그대로, 어디에도 없을 때만 자리표시. 출력: PATCH 본문 → $tmp, id/url → stdout.
    meta="$(python3 - "$1" "${2:0:7}" "$(date '+%Y-%m-%d')" "$(deployer)" "${3:-없음}" "$tmp" "$tmp.in" <<'PY'
import json, re, sys
tag, sha, released, by, prev, out, src = sys.argv[1:8]
r = json.load(open(src)); body = r.get("body") or ""
# Approved by = 릴리스를 발행한 GitHub 계정 (Publish 버튼을 누른 사람). 손으로 이미 써 뒀으면 그대로,
# 자리표시("(릴리스 발행자)")만 있으면 바꾼다.
publisher = (r.get("author") or {}).get("login") or "(릴리스 발행자)"
vals = [("Version", tag), ("Commit", sha), ("Released", released), ("Deployed by", by),
        ("Previous version", prev), ("Rollback target", prev)]
missing = []
for k, v in vals:
    body, n = re.subn(rf"^{re.escape(k)}:.*$", f"{k}: {v}", body, count=1, flags=re.M)
    if n == 0:
        missing.append(f"{k}: {v}")
m = re.search(r"^Approved by:(.*)$", body, re.M)
if m and "(릴리스 발행자)" in m.group(1):
    body = body[:m.start()] + f"Approved by: {publisher}" + body[m.end():]
elif not m:
    rel = re.search(r"^Released:.*$", body, re.M)
    if rel and not missing:
        # 기록 줄은 다 있는데 Approved 만 없다 — 그 자리(Released 다음)에 끼워 넣는다
        body = body[:rel.end()] + f"\nApproved by: {publisher}" + body[rel.end():]
    else:
        missing.insert(3 if len(missing) >= 3 else len(missing), f"Approved by: {publisher}")
if missing:
    body = body.rstrip("\n") + "\n\n## 배포 기록\n" + "\n".join(missing) + "\n"
json.dump({"body": body}, open(out, "w"), ensure_ascii=False)
print(f"{r['id']}\t{r.get('html_url', '')}")
PY
)" || { rm -f "$tmp" "$tmp.in"; say "[record] 릴리스 설명 갱신 준비 실패 — 위 블록을 손으로 붙여 넣으세요." >&2; return 0; }
    rid="${meta%%$'\t'*}"; url="${meta#*$'\t'}"
    if gh_cmd api -X PATCH "repos/{owner}/{repo}/releases/$rid" --input "$tmp" >/dev/null 2>&1; then
        say "[record] GitHub 릴리스 설명에 배포 기록을 채웠습니다: $url"
    else
        say "[record] GitHub 릴리스 설명 갱신 실패 — 위 블록을 손으로 붙여 넣으세요." >&2
    fi
    rm -f "$tmp" "$tmp.in"
}

# 릴리스 체크리스트(RELEASE.md)가 요구하는 기록, 채워서 출력 — GitHub Release 에 붙여넣는다.
# 서버 계정은 공용이라 DEPLOY_BY (scripts/env.local) 로 사람 이름을 넣는다.
release_record() {  # $1 = 태그  $2 = sha  $3 = 직전 운영 태그 (없으면 "")
    say
    say "[record] 배포 기록:"
    say "         Version: $1"
    say "         Commit: ${2:0:7}"
    say "         Released: $(date '+%Y-%m-%d')"
    say "         Approved by: (릴리스 발행자 — GitHub 릴리스에는 발행 계정으로 기입)"
    say "         Deployed by: $(deployer)"
    say "         Previous version: ${3:-없음}"
    say "         Rollback target: ${3:-없음}"
    update_release_record "$1" "$2" "$3"
}

# 배포 시도 전의 체크아웃으로 복귀 (브랜치였으면 브랜치로).
restore_checkout() {  # $1 = 브랜치 이름 또는 "", $2 = sha
    if [ -n "$1" ]; then g checkout --quiet "$1"; else g checkout --quiet --detach "$2"; fi
}

# 체크아웃을 되돌린 뒤 의존성도 그 커밋 기준으로 되돌린다 — install_deps 가 이미 새 태그의 의존성을
# 깔았을 수 있고(npm ci 는 node_modules 를 비우고 시작), 코드만 되돌리면 실행 환경이 어긋난다.
# 실패해도 계속(0) — 대신 복구 명령을 알려 준다.
restore_deps() {  # $1 = 되돌린 sha
    if install_deps all "$1" >/dev/null 2>&1; then
        say "[undo]   이전 의존성으로 복구됨." >&2
    else
        say "[error]  이전 의존성 복구 실패 — node_modules/.venv 가 어긋나 있을 수 있음. 네트워크 확인 뒤: ./scripts/deploy.sh deps" >&2
    fi
    return 0
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
    prev_tag="$(previous_of "$tag")"

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

    if ! install_deps "$pre_sha" "$sha"; then
        record "$action" "$tag" "$sha" deps-failed
        if [ "$action" = auto-rollback ]; then
            # 롤백 중에 실패: 새(깨진) 태그로 되돌아가느니 롤백 대상 코드에 머문다.
            restore_maint "$maint_saved"; trap - EXIT
            say "[error]  롤백 대상 $tag 의 의존성 설치 실패 — 체크아웃은 $tag 에 둠. 복구: ./scripts/deploy.sh deps && ./scripts/restart-all.sh" >&2
            return 3
        fi
        restore_checkout "$pre_branch" "$pre_sha"
        say "[undo]   의존성 설치 실패, 서버는 그대로 — 체크아웃을 ${pre_branch:-${pre_sha:0:7}} 로 되돌리고 그 의존성으로 재설치 시도" >&2
        restore_deps "$pre_sha"
        restore_maint "$maint_saved"
        trap - EXIT
        return 3
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
            # 프론트 빌드 실패 — restart-all.sh 가 서버를 건드리지 않았다. 의존성은 이미 새 태그 것으로
            # 바뀌었을 수 있으니 코드와 함께 되돌린다.
            record "$action" "$tag" "$sha" build-failed
            restore_checkout "$pre_branch" "$pre_sha"
            say "[undo]   프론트 빌드 실패, 서버는 그대로 — 체크아웃을 ${pre_branch:-${pre_sha:0:7}} 로 되돌리고 그 의존성으로 재설치" >&2
            restore_deps "$pre_sha"
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
                restore_deps "$pre_sha"   # 재기동 전에 되돌린 커밋의 의존성으로
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
    # 그 커밋이 이미 체크아웃돼 있고 백엔드도 그 커밋으로 떠서 응답 중이면 재기동할 이유가 없다 —
    # 기록만 남긴다 (첫 릴리스: install-units.sh --move 로 main 을 띄운 직후 같은 커밋에 태그가 붙는 경우).
    local rsha
    rsha="$(running_sha)"
    if [ "$TAG_SHA" = "$(g rev-parse HEAD)" ] && [ -n "$rsha" ] && [ "${TAG_SHA:0:${#rsha}}" = "$rsha" ] && backend_alive; then
        say "[deploy] 그 커밋이 이미 떠 있고 응답 중 — 재기동 없이 기록만 남깁니다."
        if [ "$dry_run" = 1 ]; then say "[dry-run] 아무것도 바꾸지 않았습니다."; return 0; fi
        local prev; prev="$(previous_of "$tag")"
        record deploy "$tag" "$TAG_SHA" "ok (재기동 없음)"
        say "[done]   $tag (${TAG_SHA:0:7}) 운영 중."
        release_record "$tag" "$TAG_SHA" "$prev"
        return 0
    fi
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
    fetch_tags --soft   # 방금 발행된 태그가 보이도록; 못 닿으면 마지막 fetch 기준
    read_deployed
    local head_sha head_ref
    head_sha="$(g rev-parse HEAD)"
    if [ -n "$DEP_TAG" ] && [ "$head_sha" = "$DEP_SHA" ]; then
        head_ref="$DEP_TAG"
    else
        head_ref="$(head_release_tag)"
        [ -n "$head_ref" ] || head_ref="$(g symbolic-ref --short -q HEAD || echo detached)"
    fi

    local wd
    wd="$(units_serving_dir)"
    if [ -n "$wd" ] && [ "$wd" != "$REPO" ]; then
        say "[folder]   여기는 운영 폴더가 아닙니다 — 유닛은 $wd 를 서비스합니다. 아래는 이 폴더 기준."
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
    local run_sha
    run_sha="$(running_sha)"
    if [ -n "$run_sha" ]; then
        if [ -n "$DEP_SHA" ] && [ "${DEP_SHA:0:${#run_sha}}" != "$run_sha" ]; then
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
    check_serving_repo   # 플래그는 폴더별 — 개발 폴더에 세우면 운영 healthcheck 는 못 보고 Worker 페이지만 켜진다
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

cmd_deps() {
    check_serving_repo
    say "[deps]   현재 체크아웃($(g rev-parse --short HEAD))의 의존성을 강제로 설치합니다"
    install_deps all "$(g rev-parse HEAD)" || die "의존성 설치 실패."
    say "[deps]   완료 — 적용하려면 재기동: ./scripts/restart-all.sh"
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
        deps)      cmd_deps ;;
        v*)        cmd_deploy "$cmd" ;;
        *)         echo "알 수 없는 명령: $cmd" >&2; usage >&2; exit 2 ;;
    esac
}

main "$@"
