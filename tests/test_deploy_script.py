"""Tests for scripts/deploy.sh — 릴리스 태그 배포 / 롤백 / 점검 모드 / 상태.

실제 재기동(restart-all.sh)은 스택을 건드리므로 스텁으로 바꿔 호출 기록과 종료 코드만
흉내 낸다(0 정상, 3 프론트 빌드 실패, 1 재기동 후 실패). tmp 디렉터리에 bare origin +
운영 체크아웃 역할의 클론을 만들어 git 동작(태그 검증·체크아웃·복원)은 진짜로 돌린다.
DEPLOY_SKIP_UNIT_CHECK=1 로 "systemd 가 서비스하는 체크아웃인가" 가드만 건너뛴다.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# 실제 restart-all.sh 처럼 종료 시 maintenance 플래그를 지운다(플래그 복원 로직 검증용).
# 종료 코드는 레포 루트의 STUB_RC 첫 줄을 한 번 쓰고 지운다 — "첫 호출 실패, 롤백 호출 성공" 시나리오.
STUB_RESTART = """#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd)"
m=0; [ -f "$root/logs/run/maintenance" ] && m=1
echo "restart $(git -C "$root" rev-parse --short HEAD) maint=$m" >>"$root/logs/run/restart-calls"
rm -f "$root/logs/run/maintenance"
[ -f "$root/STUB_TOUCH" ] && echo blocker >"$root/$(cat "$root/STUB_TOUCH")"
rc="$(sed -n 1p "$root/STUB_RC" 2>/dev/null || true)"
[ -f "$root/STUB_RC" ] && sed -i 1d "$root/STUB_RC"
exit "${rc:-0}"
"""
STUB_HEALTH = "#!/usr/bin/env bash\necho '[stub] ok'\n"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def prod(tmp_path):
    """bare origin(main: first→second→third, 태그 v0.1.0-beta=first, v0.2.0-beta=second)
    + 그 클론(운영 체크아웃 역할, HEAD=main=third 즉 아직 릴리스 안 된 커밋)."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "t@example.com")
    git(src, "config", "user.name", "t")
    (src / "scripts").mkdir()
    (src / "scripts" / "_common.sh").write_bytes((SCRIPTS / "_common.sh").read_bytes())
    _executable(src / "scripts" / "deploy.sh", (SCRIPTS / "deploy.sh").read_text(encoding="utf-8"))
    _executable(src / "scripts" / "restart-all.sh", STUB_RESTART)
    _executable(src / "scripts" / "healthcheck.sh", STUB_HEALTH)
    (src / ".gitignore").write_text("logs/\nSTUB_RC\nSTUB_TOUCH\n")
    (src / "app.txt").write_text("first\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    git(src, "tag", "v0.1.0-beta")
    (src / "app.txt").write_text("second\n")
    git(src, "commit", "-qam", "second (#2)")
    git(src, "tag", "v0.2.0-beta")
    (src / "app.txt").write_text("third\n")
    git(src, "commit", "-qam", "third, unreleased (#3)")

    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    clone = tmp_path / "prod"
    git(tmp_path, "clone", "-q", str(bare), str(clone))
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    return clone


def run(root, *args, check=True, env=None):
    e = os.environ.copy()
    e["DEPLOY_SKIP_UNIT_CHECK"] = "1"
    e.pop("CLOUDFLARE_API_TOKEN", None)          # the Worker step must never run from tests
    e.update(env or {})
    p = subprocess.run(
        ["bash", str(root / "scripts" / "deploy.sh"), *args],
        capture_output=True, text=True, env=e, cwd=root, stdin=subprocess.DEVNULL,
    )
    if check and p.returncode != 0:
        raise AssertionError(f"deploy.sh {args} failed rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def head(root):
    return git(root, "rev-parse", "HEAD")


def on_branch(root):
    """현재 브랜치 이름, detached HEAD 면 빈 문자열."""
    p = subprocess.run(["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def tag_sha(root, tag):
    return git(root, "rev-parse", f"{tag}^{{commit}}")


def restart_calls(root):
    f = root / "logs" / "run" / "restart-calls"
    return f.read_text().splitlines() if f.exists() else []


def deployed(root):
    """[tag, sha] of the last successful row in deploys.log (what deploy.sh calls live)."""
    ok = [r for r in log_rows(root) if r[4].startswith("ok")]
    return ok[-1][2:4] if ok else []


def origin(root):
    return root.parent / "origin.git"


def log_rows(root):
    f = root / "logs" / "run" / "deploys.log"
    return [line.split("\t") for line in f.read_text().splitlines()] if f.exists() else []


class TestDeploy:
    def test_checks_out_tag_restarts_and_records(self, prod):
        p = run(prod, "v0.1.0-beta", "--yes")
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert on_branch(prod) == ""                                     # detached at the tag
        assert deployed(prod)[:2] == ["v0.1.0-beta", tag_sha(prod, "v0.1.0-beta")]
        assert restart_calls(prod) == [f"restart {tag_sha(prod, 'v0.1.0-beta')[:7]} maint=1"]
        assert not (prod / "logs" / "run" / "maintenance").exists()
        rows = log_rows(prod)
        assert rows[-1][1:3] == ["deploy", "v0.1.0-beta"] and rows[-1][4] == "ok"
        assert "운영 중" in p.stdout
        # the release record to paste into the GitHub Release
        assert "Version: v0.1.0-beta" in p.stdout and f"Commit: {tag_sha(prod, 'v0.1.0-beta')[:7]}" in p.stdout
        assert "Previous version: 없음" in p.stdout and "Rollback target: 없음" in p.stdout

    def test_more_than_40_commits_does_not_kill_the_script(self, prod):
        # `git log | head -40` under pipefail died with SIGPIPE; --max-count must not
        for i in range(45):
            (prod / "app.txt").write_text(f"bulk {i}\n")
            git(prod, "commit", "-qam", f"bulk commit {i}")
        git(prod, "tag", "v0.5.0-beta")
        git(prod, "push", "-q", "origin", "main", "v0.5.0-beta")
        run(prod, "v0.1.0-beta", "--yes")
        p = run(prod, "v0.5.0-beta", "--yes")
        assert head(prod) == tag_sha(prod, "v0.5.0-beta")
        assert p.stdout.count("bulk commit") == 40

    def test_shows_commits_going_live(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        p = run(prod, "v0.2.0-beta", "--yes", env={"DEPLOY_BY": "tester"})
        assert "새로 나가는 커밋" in p.stdout and "second (#2)" in p.stdout
        assert head(prod) == tag_sha(prod, "v0.2.0-beta")
        assert "Previous version: v0.1.0-beta" in p.stdout and "Rollback target: v0.1.0-beta" in p.stdout
        assert "Deployed by: tester" in p.stdout
        assert "[record]" not in run(prod, "rollback").stdout           # rollback is not a release

    def test_older_tag_lists_removed_commits(self, prod):
        # HEAD is main (third) and nothing is recorded, so v0.1.0-beta is a step backwards.
        p = run(prod, "v0.1.0-beta", "--yes")
        assert "빠지는 커밋" in p.stdout and "third, unreleased (#3)" in p.stdout

    def test_unknown_tag_refused(self, prod):
        before = head(prod)
        p = run(prod, "v9.9.9-beta", "--yes", check=False)
        assert p.returncode != 0 and "origin 에 없습니다" in p.stderr
        assert head(prod) == before and restart_calls(prod) == []

    def test_bad_tag_name_refused(self, prod):
        p = run(prod, "v1", "--yes", check=False)
        assert p.returncode != 0 and "릴리스 태그 이름이 아닙니다" in p.stderr
        assert restart_calls(prod) == []

    def test_tag_not_merged_into_main_refused(self, prod):
        git(prod, "checkout", "-q", "-b", "side", "v0.1.0-beta")
        (prod / "app.txt").write_text("side\n")
        git(prod, "commit", "-qam", "side work")
        git(prod, "tag", "v0.3.0-beta")
        git(prod, "push", "-q", "origin", "v0.3.0-beta")
        git(prod, "checkout", "-q", "main")
        p = run(prod, "v0.3.0-beta", "--yes", check=False)
        assert p.returncode != 0 and "main 에 머지돼 있지 않습니다" in p.stderr
        assert restart_calls(prod) == []

    def test_local_only_tag_is_not_a_release(self, prod):
        # `git tag` on the box, never published on GitHub — must not deploy, and gets pruned
        git(prod, "tag", "v0.9.0-beta", "main")
        p = run(prod, "v0.9.0-beta", "--yes", check=False)
        assert p.returncode != 0 and "origin 에 없습니다" in p.stderr
        assert restart_calls(prod) == []
        assert git(prod, "tag", "-l", "v0.9.0-beta") == ""

    def test_tag_deleted_on_origin_is_refused_even_if_fetched_before(self, prod):
        run(prod, "v0.1.0-beta", "--yes")                  # fetched every tag, incl. v0.2.0-beta
        git(origin(prod), "tag", "-d", "v0.2.0-beta")
        p = run(prod, "v0.2.0-beta", "--yes", check=False)
        assert p.returncode != 0 and "origin 에 없습니다" in p.stderr
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert "v0.2.0-beta" not in run(prod, "tags").stdout

    def test_tag_moved_on_origin_deploys_the_new_commit(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        git(origin(prod), "tag", "-f", "v0.2.0-beta", "main")   # re-cut onto "third"
        p = run(prod, "v0.2.0-beta", "--yes")
        assert head(prod) == tag_sha(prod, "main")
        assert "third, unreleased (#3)" in p.stdout

    def test_dirty_tracked_file_refused(self, prod):
        (prod / "app.txt").write_text("local edit\n")
        p = run(prod, "v0.1.0-beta", "--yes", check=False)
        assert p.returncode != 0 and "커밋 안 된 수정" in p.stderr
        assert "app.txt" in p.stderr and restart_calls(prod) == []

    def test_dry_run_changes_nothing(self, prod):
        before = head(prod)
        p = run(prod, "v0.1.0-beta", "--dry-run")
        assert "아무것도 바꾸지 않았습니다" in p.stdout
        assert head(prod) == before and restart_calls(prod) == [] and deployed(prod) == []

    def test_unknown_flag_is_an_error_not_help(self, prod):
        p = run(prod, "v0.1.0-beta", "--yes", "--typo", check=False)
        assert p.returncode != 0 and "알 수 없는 플래그: --typo" in p.stderr
        assert restart_calls(prod) == [] and head(prod) == tag_sha(prod, "main")

    def test_without_yes_needs_a_terminal(self, prod):
        p = run(prod, "v0.1.0-beta", check=False)   # stdin is /dev/null
        assert p.returncode != 0 and "--yes" in p.stderr
        assert restart_calls(prod) == []


class TestServingFolder:
    """유닛이 다른 폴더를 서비스하면 배포·점검 모드는 거부, status 는 알려 준다 (CAMCHAT_UNITS_DIR 훅)."""

    def _run(self, prod, *args, units_dir, check=False):
        e = os.environ.copy()
        e.pop("DEPLOY_SKIP_UNIT_CHECK", None)
        e.pop("CLOUDFLARE_API_TOKEN", None)
        e["CAMCHAT_UNITS_DIR"] = units_dir
        p = subprocess.run(["bash", str(prod / "scripts" / "deploy.sh"), *args],
                           capture_output=True, text=True, env=e, cwd=prod, stdin=subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise AssertionError(f"deploy.sh {args} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def test_deploy_refused_outside_the_serving_folder(self, prod):
        p = self._run(prod, "v0.1.0-beta", "--yes", units_dir="/srv/elsewhere")
        assert p.returncode != 0 and "운영 폴더에서 실행하세요: cd /srv/elsewhere" in p.stderr
        assert restart_calls(prod) == [] and on_branch(prod) == "main"

    def test_maint_refused_outside_the_serving_folder(self, prod):
        p = self._run(prod, "maint", "on", "--local-only", units_dir="/srv/elsewhere")
        assert p.returncode != 0 and "운영 폴더에서 실행하세요" in p.stderr
        assert not (prod / "logs" / "run" / "maintenance").exists()

    def test_status_says_which_folder_is_serving(self, prod):
        p = self._run(prod, "status", units_dir="/srv/elsewhere", check=True)
        assert "[folder]   여기는 운영 폴더가 아닙니다 — 유닛은 /srv/elsewhere 를 서비스합니다" in p.stdout

    def test_serving_folder_itself_is_allowed(self, prod):
        self._run(prod, "v0.1.0-beta", "--yes", units_dir=str(prod), check=True)
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")

    def test_no_units_at_all_is_allowed(self, prod):
        self._run(prod, "v0.1.0-beta", "--yes", units_dir="", check=True)
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")


class TestSameCommitAlreadyRunning:
    """그 커밋이 이미 체크아웃돼 있고 백엔드도 그 커밋으로 응답 중이면 재기동 없이 기록만."""

    def _running(self, prod, sha):
        (prod / "logs" / "backend").mkdir(parents=True, exist_ok=True)
        (prod / "logs" / "backend" / "server.out").write_text(f"[backend] starting on :8000 ({sha[:7]})\n")

    def test_records_without_restart(self, prod):
        git(prod, "checkout", "-q", "--detach", "v0.1.0-beta")     # install-units.sh --move 가 띄운 상태
        self._running(prod, tag_sha(prod, "v0.1.0-beta"))
        p = run(prod, "v0.1.0-beta", "--yes", env={"DEPLOY_HEALTH_CMD": "true"})
        assert "재기동 없이 기록만" in p.stdout and "Version: v0.1.0-beta" in p.stdout
        assert restart_calls(prod) == []
        assert deployed(prod)[0] == "v0.1.0-beta" and log_rows(prod)[-1][4] == "ok (재기동 없음)"

    def test_restarts_when_backend_not_answering(self, prod):
        git(prod, "checkout", "-q", "--detach", "v0.1.0-beta")
        self._running(prod, tag_sha(prod, "v0.1.0-beta"))
        run(prod, "v0.1.0-beta", "--yes", env={"DEPLOY_HEALTH_CMD": "false"})
        assert len(restart_calls(prod)) == 1

    def test_restarts_when_running_commit_differs(self, prod):
        git(prod, "checkout", "-q", "--detach", "v0.1.0-beta")
        self._running(prod, tag_sha(prod, "v0.2.0-beta"))           # 폴더는 v0.1 인데 도는 건 v0.2
        run(prod, "v0.1.0-beta", "--yes", env={"DEPLOY_HEALTH_CMD": "true"})
        assert len(restart_calls(prod)) == 1


class TestFailureHandling:
    def test_build_failure_restores_branch_without_restart(self, prod):
        (prod / "STUB_RC").write_text("3\n")
        p = run(prod, "v0.1.0-beta", "--yes", check=False)
        assert p.returncode == 3
        assert on_branch(prod) == "main"                                 # back on the branch it left
        assert len(restart_calls(prod)) == 1                             # the failed attempt only
        assert deployed(prod) == []
        assert log_rows(prod)[-1][4] == "build-failed"

    def test_build_failure_restores_previous_tag(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        (prod / "STUB_RC").write_text("3\n")
        p = run(prod, "v0.2.0-beta", "--yes", check=False)
        assert p.returncode == 3
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert deployed(prod)[0] == "v0.1.0-beta"

    def test_restart_failure_rolls_back_to_previous_deploy(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        (prod / "STUB_RC").write_text("1\n")           # first restart fails, rollback restart succeeds
        p = run(prod, "v0.2.0-beta", "--yes", check=False)
        assert p.returncode == 1
        assert "v0.1.0-beta 로 롤백합니다" in p.stderr
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert deployed(prod)[0] == "v0.1.0-beta"
        assert len(restart_calls(prod)) == 3           # deploy #1, failed deploy #2, rollback
        results = [r[4] for r in log_rows(prod)]
        assert results[-2].startswith("restart-failed") and results[-1] == "ok"
        assert log_rows(prod)[-1][1] == "auto-rollback"

    def test_restart_failure_on_first_deploy_returns_to_previous_checkout(self, prod):
        before = head(prod)                            # main, nothing recorded yet
        (prod / "STUB_RC").write_text("1\n")
        p = run(prod, "v0.1.0-beta", "--yes", check=False)
        assert p.returncode == 1 and "복원: main" in p.stderr
        assert head(prod) == before and on_branch(prod) == "main"
        assert len(restart_calls(prod)) == 2
        assert deployed(prod) == []                            # a branch is not a release
        assert log_rows(prod)[-1][1] == "auto-rollback" and log_rows(prod)[-1][4].startswith("restored")
        assert "첫 배포 전" in run(prod, "status").stdout
        assert "이전 배포 기록이 없습니다" in run(prod, "rollback", check=False).stderr

    def test_checkout_failure_during_rollback_is_not_swallowed(self, prod):
        # live = v0.3.0-beta (has extra.txt). Deploying v0.1.0-beta removes extra.txt; the
        # stub then drops an untracked extra.txt and fails, so the rollback checkout to
        # v0.3.0-beta collides. That must surface, not restart the broken tag as "ok".
        (prod / "extra.txt").write_text("x\n")
        git(prod, "add", "extra.txt")
        git(prod, "commit", "-qm", "fourth: extra file")
        git(prod, "tag", "v0.3.0-beta")
        git(prod, "push", "-q", "origin", "main", "v0.3.0-beta")
        run(prod, "v0.3.0-beta", "--yes")
        (prod / "STUB_RC").write_text("1\n")
        (prod / "STUB_TOUCH").write_text("extra.txt")
        p = run(prod, "v0.1.0-beta", "--yes", check=False)
        assert p.returncode == 1 and "checkout v0.3.0-beta 실패" in p.stderr
        assert len(restart_calls(prod)) == 2                   # v0.3 deploy + failed v0.1; no third
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")      # left where git left it
        assert deployed(prod)[0] == "v0.3.0-beta"              # never recorded the broken state as ok
        assert log_rows(prod)[-1][4] == "checkout-failed"

    def test_llm_probe_failure_keeps_deploy_and_warns(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        (prod / "STUB_RC").write_text("4\n")           # backend up, /health/llm failed
        p = run(prod, "v0.2.0-beta", "--yes", check=False)
        assert p.returncode == 4 and "롤백하지 않습니다" in p.stderr
        assert head(prod) == tag_sha(prod, "v0.2.0-beta")
        assert deployed(prod)[0] == "v0.2.0-beta"           # it IS live, degraded
        assert len(restart_calls(prod)) == 2                 # no rollback bounce
        assert log_rows(prod)[-1][4] == "ok (llm probe failed)"
        assert "[live]     v0.2.0-beta" in run(prod, "status").stdout

    def test_no_auto_rollback_flag_stops_after_failure(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        (prod / "STUB_RC").write_text("1\n")
        p = run(prod, "v0.2.0-beta", "--yes", "--no-auto-rollback", check=False)
        assert p.returncode == 1 and "자동 롤백 꺼짐" in p.stderr
        assert head(prod) == tag_sha(prod, "v0.2.0-beta")   # left where it failed, for inspection
        assert len(restart_calls(prod)) == 2
        assert deployed(prod)[0] == "v0.1.0-beta"           # DEPLOYED only moves on success


class TestRollback:
    def test_rollback_goes_to_previous_successful_deploy(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        run(prod, "v0.2.0-beta", "--yes")
        p = run(prod, "rollback")
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert deployed(prod)[0] == "v0.1.0-beta"
        assert log_rows(prod)[-1][1:3] == ["rollback", "v0.1.0-beta"]
        assert "대체 대상: v0.2.0-beta" in p.stdout

    def test_rollback_twice_keeps_going_back_not_forward(self, prod):
        git(prod, "tag", "v0.3.0-beta", "main")
        git(prod, "push", "-q", "origin", "v0.3.0-beta")
        run(prod, "v0.1.0-beta", "--yes")
        run(prod, "v0.2.0-beta", "--yes")
        run(prod, "v0.3.0-beta", "--yes")
        run(prod, "rollback")
        assert deployed(prod)[0] == "v0.2.0-beta"
        run(prod, "rollback")                                   # "v0.2 is bad too"
        assert deployed(prod)[0] == "v0.1.0-beta"              # not back onto v0.3
        p = run(prod, "rollback", check=False)
        assert p.returncode != 0 and "이전 배포 기록이 없습니다" in p.stderr
        run(prod, "v0.2.0-beta", "--yes")                       # an explicit deploy re-endorses it
        run(prod, "rollback")
        assert deployed(prod)[0] == "v0.1.0-beta"

    def test_rollback_without_history_needs_explicit_tag(self, prod):
        p = run(prod, "rollback", check=False)
        assert p.returncode != 0 and "이전 배포 기록이 없습니다" in p.stderr
        run(prod, "rollback", "v0.1.0-beta")
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")

    def test_rollback_stashes_uncommitted_edits_instead_of_refusing(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        run(prod, "v0.2.0-beta", "--yes")
        (prod / "app.txt").write_text("someone's 3 a.m. edit\n")
        p = run(prod, "rollback")
        assert "치워 뒀습니다" in p.stderr and "git stash pop" in p.stderr
        assert head(prod) == tag_sha(prod, "v0.1.0-beta")
        assert (prod / "app.txt").read_text() == "first\n"          # tree equals the tag
        assert "deploy.sh rollback" in git(prod, "stash", "list")    # the edit is recoverable

    def test_deploy_still_refuses_dirty_tree(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        (prod / "app.txt").write_text("edit\n")
        p = run(prod, "v0.2.0-beta", "--yes", check=False)
        assert p.returncode != 0 and "커밋 안 된 수정" in p.stderr
        assert git(prod, "stash", "list") == ""

    def test_rollback_dry_run(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        run(prod, "v0.2.0-beta", "--yes")
        p = run(prod, "rollback", "--dry-run")
        assert "아무것도 바꾸지 않았습니다" in p.stdout and head(prod) == tag_sha(prod, "v0.2.0-beta")


class TestStatusAndTags:
    def test_status_before_any_deploy(self, prod):
        p = run(prod, "status")
        assert "첫 배포 전" in p.stdout
        assert "origin 최신 태그: v0.2.0-beta (운영 중 아님)" in p.stdout

    def test_status_after_deploy_and_drift(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        p = run(prod, "status")
        assert "[live]     v0.1.0-beta" in p.stdout
        assert "[checkout] v0.1.0-beta" in p.stdout and "!= 운영중" not in p.stdout
        assert "deploy.sh v0.2.0-beta" in p.stdout
        # a developer checks out main in the served folder: status must shout
        git(prod, "checkout", "-q", "main")
        p = run(prod, "status")
        assert "[checkout] main" in p.stdout and "!= 운영중" in p.stdout

    def test_tags_newest_first_marks_live(self, prod):
        run(prod, "v0.1.0-beta", "--yes")
        p = run(prod, "tags")
        lines = p.stdout.splitlines()
        assert lines[0].startswith("v0.2.0-beta") and "<- 운영중" not in lines[0]
        assert lines[1].startswith("v0.1.0-beta") and "<- 운영중" in lines[1]

    def test_non_release_tag_names_are_ignored(self, prod):
        # a stray "vfoo" tag on main must not become the "newest" suggestion nor be listed
        git(prod, "tag", "vfoo", "main")
        git(prod, "push", "-q", "origin", "vfoo")
        run(prod, "v0.1.0-beta", "--yes")
        assert "vfoo" not in run(prod, "status").stdout
        assert "vfoo" not in run(prod, "tags").stdout
        assert "origin 최신 태그: v0.2.0-beta" in run(prod, "status").stdout
        git(prod, "tag", "wip", "v0.1.0-beta")                 # checkpoint tag on the live commit
        git(prod, "push", "-q", "origin", "wip")
        out = run(prod, "status").stdout
        assert "[checkout] v0.1.0-beta" in out and "wip" not in out

    def test_phase_suffixes_sort_alpha_before_beta_before_release(self, prod):
        for t in ("v0.3.0-alpha", "v0.3.0-beta", "v0.3.0", "v0.4.0-alpha"):
            git(prod, "tag", t, "main")
        git(prod, "push", "-q", "origin", "--tags")
        names = [line.split()[0] for line in run(prod, "tags").stdout.splitlines()]
        assert names[:4] == ["v0.4.0-alpha", "v0.3.0", "v0.3.0-beta", "v0.3.0-alpha"]
        assert "origin 최신 태그: v0.4.0-alpha" in run(prod, "status").stdout
        run(prod, "v0.3.0-beta", "--yes")
        assert "[checkout] v0.3.0-beta" in run(prod, "status").stdout   # not the newest tag on the commit

    def test_help_and_unknown_command(self, prod):
        assert "deploy.sh" in run(prod, "help").stdout
        p = run(prod, "bogus", check=False)
        assert p.returncode == 2


class TestMaintenance:
    def test_local_only_toggles_flag(self, prod):
        flag = prod / "logs" / "run" / "maintenance"
        run(prod, "maint", "on", "--local-only")
        assert flag.exists() and "deploy.sh maint on" in flag.read_text()
        assert "[maint]    ON" in run(prod, "status").stdout
        run(prod, "maint", "off", "--local-only")
        assert not flag.exists()

    def test_worker_failure_keeps_local_flag_and_reports(self, prod):
        # no Cloudflare credentials (and no worker/ here) → the Worker step must fail fast,
        # never block on an interactive wrangler login; the local flag is still set
        p = run(prod, "maint", "on", check=False, env={"HOME": str(prod)})
        assert p.returncode == 1 and "CLOUDFLARE_API_TOKEN" in p.stderr
        assert (prod / "logs" / "run" / "maintenance").exists()

    def test_off_resumes_healthcheck_even_when_worker_step_fails(self, prod):
        run(prod, "maint", "on", "--local-only")
        p = run(prod, "maint", "off", check=False, env={"HOME": str(prod)})
        assert p.returncode == 1 and "아직 점검 페이지" in p.stderr
        assert not (prod / "logs" / "run" / "maintenance").exists()

    def test_manual_flag_survives_a_deploy(self, prod):
        run(prod, "maint", "on", "--local-only")
        flag = prod / "logs" / "run" / "maintenance"
        content = flag.read_text()
        run(prod, "v0.1.0-beta", "--yes")             # the stub deletes the flag like restart-all.sh does
        assert flag.exists() and flag.read_text() == content

    def test_bad_mode(self, prod):
        assert run(prod, "maint", "sideways", check=False).returncode != 0
