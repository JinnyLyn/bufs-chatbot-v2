#!/usr/bin/env bash
# deploy.sh — the one production switch. Deploy a release tag, roll back, flip
# maintenance mode, or show what is live. Everything else (build, unit bounce, health
# probe) is delegated to restart-all.sh; nothing here starts a process on its own.
#
#   ./scripts/deploy.sh v0.2.0-beta          # deploy that release tag (shows what changes, asks y/N)
#   ./scripts/deploy.sh rollback             # back to the previous deploy — no questions asked
#   ./scripts/deploy.sh rollback v0.1.0-beta # back to a specific tag
#   ./scripts/deploy.sh status               # live tag vs checkout vs running process, health, maint
#   ./scripts/deploy.sh tags                 # release tags on origin, newest first, live one marked
#   ./scripts/deploy.sh maint on|off         # planned maintenance: Worker 503 page + healthcheck stands down
#
# Flags: --yes                skip the y/N confirmation (deploy only)
#        --dry-run            run every check, change nothing
#        --no-auto-rollback   when the restart fails, stop there instead of restoring the previous version
#        --local-only         (maint) touch only the healthcheck flag, leave the Cloudflare Worker alone
#
# A deploy, in order:
#   1. fetch tags; the tag must exist, look like vX.Y.Z[-suffix], and be merged into origin/main
#   2. this checkout must be the one systemd serves, with no uncommitted tracked changes
#      (rollback instead stashes them — an emergency must not wait for someone's edits)
#   3. show the commits that will go live (or be removed) and ask
#   4. git checkout --detach <tag>
#   5. restart-all.sh — frontend rebuild when needed, stop, start, /health + /health/llm probe
#   6. append to logs/run/deploys.log — the last "ok" row there IS what is live
# restart-all.sh exit codes and what happens next:
#   0  healthy                                → recorded as live
#   3  frontend build failed, stack untouched → checkout restored, nothing restarted
#   4  backend up, /health/llm failed         → recorded as live (degraded), NO rollback, loud warning
#   1  backend not answering                  → auto-rollback to the tag that was live (or --no-auto-rollback)
#
# The release process around this script (who tags, who deploys, version names): RELEASE.md.
#
# Test hooks (same idea as healthcheck-cron.sh): DEPLOY_RESTART_CMD replaces restart-all.sh,
# DEPLOY_SKIP_UNIT_CHECK=1 skips the "this checkout is the one systemd serves" guard.

set -Eeuo pipefail

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

DEPLOY_LOG="$RUN_DIR/deploys.log"   # TSV, one row per attempt: time  action  tag  sha  result  user
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'
# The Worker maintenance page is flipped through wrangler, which on a headless box needs an
# API token (Workers KV Storage: Edit) — scripts/env.local is sourced by _common.sh, so one
# line there reaches every npm/wrangler call below.
WRANGLER_AUTH_HINT='put "export CLOUDFLARE_API_TOKEN=..." in scripts/env.local, or run: cd worker && npx wrangler login'

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
# state — deploys.log is the single record; "live" = its last row whose result starts "ok"
# ---------------------------------------------------------------------------------------

# Sets DEP_TAG / DEP_SHA / DEP_TIME (all empty when nothing has been deployed yet).
read_deployed() {
    DEP_TAG=""; DEP_SHA=""; DEP_TIME=""
    [ -f "$DEPLOY_LOG" ] || return 0
    IFS=$'\t' read -r DEP_TAG DEP_SHA DEP_TIME < <(
        awk -F'\t' '$5 ~ /^ok/ { t = $3; s = $4; d = $1 } END { printf "%s\t%s\t%s\n", t, s, d }' "$DEPLOY_LOG"
    ) || true
}

record() {  # $1 action  $2 tag  $3 sha  $4 result
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(now)" "$1" "$2" "$3" "$4" "${USER:-?}" >>"$DEPLOY_LOG"
}

# The tag to roll back to: the newest successful deploy whose tag differs from the live one.
previous_tag() {
    read_deployed
    [ -f "$DEPLOY_LOG" ] || return 0
    awk -F'\t' -v cur="$DEP_TAG" '$5 ~ /^ok/ && $3 != cur { t = $3 } END { print t }' "$DEPLOY_LOG"
}

# Release tags on origin, newest first, one per line: tag<TAB>date<TAB>subject.
# Only names that verify_tag would accept — a stray "v1" or "vfoo" tag is not a release.
release_tags() {
    g -c versionsort.suffix=-beta for-each-ref --sort=-v:refname \
        --format='%(refname:short)%09%(creatordate:short)%09%(contents:subject)' 'refs/tags/v*' \
        | awk -F'\t' -v re="$TAG_RE" '$1 ~ re'
}

latest_release_tag() { release_tags | head -1 | cut -f1; }

# ---------------------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------------------

# The systemd units hard-code the checkout they run from. Deploying from a different clone
# would switch files nobody is serving and bounce units that serve something else.
check_serving_repo() {
    [ "${DEPLOY_SKIP_UNIT_CHECK:-0}" = 1 ] && return 0
    units_installed || return 0
    local wd
    wd="$(systemctl --user show -p WorkingDirectory --value camchat-backend.service 2>/dev/null || true)"
    [ -z "$wd" ] || [ "$wd" = "$REPO" ] \
        || die "systemd serves $wd, but this is $REPO — run deploy.sh from the served checkout."
}

dirty_files() { g status --porcelain --untracked-files=no; }

check_clean() {
    local dirty
    dirty="$(dirty_files)"
    [ -z "$dirty" ] || die "uncommitted changes in tracked files — commit or stash first, production must equal the tag exactly:"$'\n'"$dirty"
}

# Rollback is the emergency path: set someone's edits aside instead of refusing.
stash_if_dirty() {
    [ -n "$(dirty_files)" ] || return 0
    local label
    label="deploy.sh rollback $(now): uncommitted changes set aside"
    g stash push --quiet -m "$label"
    say "[stash]  uncommitted changes were in the way — stashed as \"$label\"" >&2
    say "         recover later with: git stash pop" >&2
}

fetch_tags() {
    g fetch origin --tags --prune --quiet || die "git fetch failed — no network, or origin unreachable."
}

# Validates a release tag: name pattern, exists, reachable from origin/main. Sets TAG_SHA.
verify_tag() {
    local tag="$1"
    [[ "$tag" =~ $TAG_RE ]] || die "'$tag' is not a release tag name (expected vX.Y.Z or vX.Y.Z-beta…, see RELEASE.md)."
    TAG_SHA="$(g rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null || true)"
    [ -n "$TAG_SHA" ] || die "tag '$tag' does not exist on origin — has the release been published on GitHub? (./scripts/deploy.sh tags)"
    g merge-base --is-ancestor "$TAG_SHA" origin/main \
        || die "tag '$tag' is not merged into main — releases are cut from main only."
}

dry_run_note() {
    say "[dry-run] would: git checkout --detach $1 && ./scripts/restart-all.sh  — nothing changed."
}

# ---------------------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------------------

run_restart() {
    if [ -n "${DEPLOY_RESTART_CMD:-}" ]; then
        # shellcheck disable=SC2086  # test hook: a command line, split on purpose
        $DEPLOY_RESTART_CMD
    else
        "$REPO/scripts/restart-all.sh"
    fi
}

# Restore the checkout that was current before a deploy attempt (branch if it was one).
restore_checkout() {  # $1 = branch name or "", $2 = sha
    if [ -n "$1" ]; then g checkout --quiet "$1"; else g checkout --quiet --detach "$2"; fi
}

# switch_to <tag> <sha> <action> <allow_rollback>
# Checks out the tag, restarts, records. On a dead backend with allow_rollback=1, restores
# the tag that was live (or the pre-deploy commit when nothing was recorded yet) once.
switch_to() {
    local tag="$1" sha="$2" action="$3" allow_rollback="$4"
    local pre_branch pre_sha maint_saved="" rc=0
    pre_branch="$(g symbolic-ref --short -q HEAD || true)"
    pre_sha="$(g rev-parse HEAD)"

    # restart-all.sh clears the maintenance flag on exit; put a manual one back afterwards.
    [ -f "$MAINT_FLAG" ] && maint_saved="$(cat "$MAINT_FLAG")"

    say "[switch] git checkout --detach $tag  (${sha:0:7})"
    g checkout --quiet --detach "$tag"

    say "[switch] restart-all.sh"
    run_restart || rc=$?

    [ -n "$maint_saved" ] && printf '%s\n' "$maint_saved" >"$MAINT_FLAG"

    case "$rc" in
        0)
            record "$action" "$tag" "$sha" ok
            say
            say "[done]   $tag (${sha:0:7}) is live.   undo: ./scripts/deploy.sh rollback"
            return 0 ;;
        3)
            # frontend build failed — restart-all.sh left the stack untouched
            record "$action" "$tag" "$sha" build-failed
            restore_checkout "$pre_branch" "$pre_sha"
            say "[undo]   build failed, stack untouched — checkout restored to ${pre_branch:-${pre_sha:0:7}}." >&2
            return 3 ;;
        4)
            # backend answers, LLM probe failed — the code IS live; bouncing again would not
            # fix ollama and would only add downtime, so keep it and shout.
            record "$action" "$tag" "$sha" "ok (llm probe failed)"
            say "[warn]   $tag (${sha:0:7}) is live but /health/llm failed — not rolling back." >&2
            say "         check ollama: ./scripts/healthcheck.sh / systemctl --user status camchat-ollama" >&2
            return 4 ;;
        *)
            record "$action" "$tag" "$sha" "restart-failed(rc=$rc)"
            if [ "$allow_rollback" != 1 ]; then
                say "[error]  restart failed (rc=$rc) and auto-rollback is off — stack may be down." >&2
                say "         fix, or: ./scripts/deploy.sh rollback" >&2
                return 1
            fi
            # Go back to what was live before this attempt (the log only gains an "ok" row on
            # success); on the very first deploy there is no record, so back to what was checked out.
            local back_tag back_sha
            read_deployed
            if [ -n "$DEP_TAG" ]; then
                back_tag="$DEP_TAG"; back_sha="$DEP_SHA"
            else
                back_tag="$pre_sha"; back_sha="$pre_sha"
            fi
            say "[undo]   restart failed (rc=$rc) — rolling back to $back_tag" >&2
            if switch_to "$back_tag" "$back_sha" auto-rollback 0; then
                say "[undo]   rolled back; $tag was NOT deployed. See logs/backend/server.err." >&2
            else
                say "[error]  rollback ALSO failed — stack is down. systemctl --user status camchat.target" >&2
            fi
            return 1 ;;
    esac
}

# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------

cmd_deploy() {  # $1 = tag
    local tag="$1" from
    check_serving_repo
    fetch_tags
    verify_tag "$tag"
    check_clean
    read_deployed

    from="${DEP_SHA:-$(g rev-parse HEAD)}"
    say "[deploy] $tag (${TAG_SHA:0:7})   currently live: ${DEP_TAG:-unrecorded} (${from:0:7})"
    if [ "$TAG_SHA" = "$from" ]; then
        say "[deploy] same commit as what is live — restart only."
    else
        if g merge-base --is-ancestor "$from" "$TAG_SHA"; then
            say "[deploy] commits going live:"
            g log --oneline --no-decorate "$from..$TAG_SHA" | head -40 | sed 's/^/           /'
        else
            say "[deploy] this tag is OLDER than what is live — commits being REMOVED:"
            g log --oneline --no-decorate "$TAG_SHA..$from" | head -40 | sed 's/^/           /'
        fi
    fi

    if [ "$dry_run" = 1 ]; then dry_run_note "$tag"; return 0; fi
    if [ "$yes" != 1 ]; then
        [ -t 0 ] || die "not a terminal — pass --yes to deploy without the prompt."
        local answer
        read -r -p "Deploy $tag to production now? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { say "aborted."; return 0; }
    fi
    switch_to "$tag" "$TAG_SHA" deploy "$auto_rollback"
}

cmd_rollback() {  # $1 = tag or ""
    local tag="${1:-}"
    check_serving_repo
    fetch_tags
    if [ -z "$tag" ]; then
        tag="$(previous_tag)"
        [ -n "$tag" ] || die "no previous deploy recorded in $DEPLOY_LOG — name the tag: ./scripts/deploy.sh rollback vX.Y.Z-beta"
    fi
    verify_tag "$tag"
    read_deployed
    say "[rollback] $tag (${TAG_SHA:0:7})   replacing: ${DEP_TAG:-unrecorded}"
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
        mark=""; [ "$tag" = "$DEP_TAG" ] && mark="   <- live"
        printf '%-18s %s  %s%s\n' "$tag" "$date" "$subject" "$mark"
    done < <(release_tags)
    [ "$n" -gt 0 ] || say "no release tags yet — publish one on GitHub (Releases -> Draft a new release), see RELEASE.md."
}

cmd_status() {
    read_deployed
    local head_sha head_ref
    head_sha="$(g rev-parse HEAD)"
    head_ref="$(head_release_tag)"
    [ -n "$head_ref" ] || head_ref="$(g symbolic-ref --short -q HEAD || echo detached)"

    if [ -n "$DEP_TAG" ]; then
        say "[live]     $DEP_TAG (${DEP_SHA:0:7})  deployed $DEP_TIME"
    else
        say "[live]     nothing recorded yet — first deploy not done (./scripts/deploy.sh <tag>)"
    fi

    if [ -z "$DEP_SHA" ] || [ "$head_sha" = "$DEP_SHA" ]; then
        say "[checkout] $head_ref (${head_sha:0:7})"
    else
        say "[checkout] $head_ref (${head_sha:0:7})  != live — a restart (healthcheck, systemd) would serve THIS, not $DEP_TAG"
    fi

    # The backend logs its commit on every start (run-backend.sh) — what is actually running.
    local run_line run_sha
    run_line="$(grep -a '\[backend\] starting on' "$LOG_DIR/backend/server.out" 2>/dev/null | tail -1 || true)"
    if [ -n "$run_line" ]; then
        run_sha="$(sed -n 's/.*(\([0-9a-f]\{7,\}\)).*/\1/p' <<<"$run_line")"
        if [ -n "$DEP_SHA" ] && [ -n "$run_sha" ] && [ "${DEP_SHA:0:${#run_sha}}" != "$run_sha" ]; then
            say "[running]  backend started from $run_sha  != live $DEP_TAG (${DEP_SHA:0:7})"
        else
            say "[running]  backend started from ${run_sha:-?}"
        fi
    fi

    if units_installed; then
        local u st since line=""
        for u in ollama backend frontend; do
            st="$(systemctl --user is-active "camchat-$u.service" 2>/dev/null || true)"
            since="$(systemctl --user show -p ActiveEnterTimestamp --value "camchat-$u.service" 2>/dev/null | cut -d' ' -f2-3)"
            line="$line  $u=$st${since:+ (since $since)}"
        done
        say "[units]  $line"
    fi

    if [ -x "$REPO/scripts/healthcheck.sh" ]; then
        "$REPO/scripts/healthcheck.sh" 2>&1 | sed 's/^/[health]   /' || true
    fi

    if [ -f "$MAINT_FLAG" ]; then
        say "[maint]    ON since: $(cat "$MAINT_FLAG")  — healthcheck auto-restart suspended (./scripts/deploy.sh maint off)"
    else
        say "[maint]    off"
    fi
    if [ -f "$REPO/worker/package.json" ]; then
        # wrangler prints the value on stdout (plus a blank line), "Value not found" on stderr
        # when the key is absent (= off), and an auth error on stderr when it cannot ask at all.
        local kv err
        err="$(mktemp)"
        kv="$(cd "$REPO/worker" && timeout 25 npm run --silent maintenance:status 2>"$err" || true)"
        kv="${kv//[$'\n\r\t ']/}"
        if [ "$kv" = on ]; then
            say "[worker]   maintenance page ON (visitors see the 503 notice)"
        elif [ -n "$kv" ] || grep -qi 'not found' "$err"; then
            say "[worker]   maintenance page off"
        else
            say "[worker]   maintenance page: unknown — wrangler cannot reach Cloudflare here ($WRANGLER_AUTH_HINT)"
        fi
        rm -f "$err"
    fi

    local newest
    newest="$(latest_release_tag)"
    if [ -n "$newest" ] && [ "$newest" != "$DEP_TAG" ]; then
        say "[tags]     newest on origin: $newest (not live)  -> ./scripts/deploy.sh $newest"
    fi

    if [ -f "$DEPLOY_LOG" ]; then
        say "[history]"
        tail -5 "$DEPLOY_LOG" | awk -F'\t' '{ printf "           %s  %-14s %-18s %s\n", $1, $2, $3, $5 }'
    fi
}

cmd_maint() {  # $1 = on|off
    local mode="$1" rc=0
    case "$mode" in
        on)
            maint_on "deploy.sh maint on (manual)"
            say "[maint]    healthcheck auto-restart suspended (flag: $MAINT_FLAG)"
            if [ "$local_only" = 1 ]; then return 0; fi
            if (cd "$REPO/worker" && npm run --silent maintenance:on); then
                say "[worker]   maintenance page ON — visitors see the 503 notice within ~60 s"
            else
                rc=1
                say "[error]  could not switch the Cloudflare Worker — $WRANGLER_AUTH_HINT" >&2
                say "         (the local flag IS set; re-run 'maint on' afterwards, or use --local-only)" >&2
            fi ;;
        off)
            if [ "$local_only" != 1 ]; then
                if (cd "$REPO/worker" && npm run --silent maintenance:off); then
                    say "[worker]   maintenance page off — visitors are back within ~60 s"
                else
                    rc=1
                    say "[error]  could not switch the Cloudflare Worker — visitors STILL see the maintenance page." >&2
                    say "         $WRANGLER_AUTH_HINT — then: cd worker && npm run maintenance:off" >&2
                fi
            fi
            maint_off
            say "[maint]    healthcheck auto-restart resumed" ;;
        *) die "usage: ./scripts/deploy.sh maint on|off [--local-only]" ;;
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
            --*)                die "unknown flag: $arg (./scripts/deploy.sh --help)" ;;
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
        *)         echo "unknown command: $cmd" >&2; usage >&2; exit 2 ;;
    esac
}

main "$@"
