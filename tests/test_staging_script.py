"""scripts/staging.sh 테스트 — origin/main 을 staging 폴더에 꺼내 띄우는 흐름.

restart-all.sh / stop-all.sh 는 STAGING_RESTART_CMD / STAGING_STOP_CMD 로 스텁하고(무엇을
어떤 커밋·BACKEND_ORIGIN 으로 불렀는지만 기록), tmp 에 bare origin + staging 클론
(.deploy-worktree 마커)을 만들어 git 동작(fetch·detached checkout·뒤처짐 계산)은 진짜로 돌린다.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "staging.sh"

STUB_RESTART = """#!/usr/bin/env bash
echo "restart $(git -C "$STAGING_DIR" rev-parse --short HEAD) origin=$BACKEND_ORIGIN" >>"$STAGING_DIR/calls"
"""
STUB_STOP = """#!/usr/bin/env bash
echo "stop" >>"$STAGING_DIR/calls"
"""
STUB_HEALTH = "#!/usr/bin/env bash\necho \"[stub] ok :$BACKEND_PORT/:$FRONTEND_PORT\"\n"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def env(tmp_path):
    """src(main: first→second) → bare origin → staging 클론(마커·env.local·스텁 healthcheck)."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "t@example.com")
    git(src, "config", "user.name", "t")
    (src / "app.txt").write_text("first\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    (src / "app.txt").write_text("second\n")
    git(src, "commit", "-qam", "second")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    staging = tmp_path / "staging"
    git(tmp_path, "clone", "-q", str(bare), str(staging))
    (staging / ".deploy-worktree").write_text("staging\n")
    (staging / "scripts").mkdir()
    (staging / "scripts" / "env.local").write_text("export BACKEND_PORT=8010\nexport FRONTEND_PORT=3010\nexport START_OLLAMA=no\n")
    _executable(staging / "scripts" / "healthcheck.sh", STUB_HEALTH)
    (staging / "logs" / "frontend").mkdir(parents=True)
    _executable(tmp_path / "stub-restart.sh", STUB_RESTART)
    _executable(tmp_path / "stub-stop.sh", STUB_STOP)
    return {"src": src, "bare": bare, "staging": staging, "tmp": tmp_path}


def run(env, *args, check=True, staging_dir=None):
    e = os.environ.copy()
    e["STAGING_DIR"] = str(staging_dir or env["staging"])
    e["STAGING_RESTART_CMD"] = f"bash {env['tmp'] / 'stub-restart.sh'}"
    e["STAGING_STOP_CMD"] = f"bash {env['tmp'] / 'stub-stop.sh'}"
    p = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise AssertionError(f"staging.sh {args} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def calls(env):
    f = env["staging"] / "calls"
    return f.read_text().splitlines() if f.exists() else []


def push_commit(env, msg):
    (env["src"] / "app.txt").write_text(msg + "\n")
    git(env["src"], "commit", "-qam", msg)
    git(env["src"], "push", "-q", str(env["bare"]), "main")
    return git(env["src"], "rev-parse", "--short", "HEAD")


class TestUp:
    def test_checks_out_origin_main_detached_and_restarts_with_backend_origin(self, env):
        p = run(env, "up")
        head = git(env["staging"], "rev-parse", "--short", "HEAD")
        assert head == git(env["src"], "rev-parse", "--short", "HEAD")
        assert subprocess.run(["git", "-C", str(env["staging"]), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0  # detached
        assert calls(env) == [f"restart {head} origin=http://localhost:8010"]
        assert "staging.maruvis.kr" in p.stdout and "staging.sh down" in p.stdout

    def test_up_follows_new_commits_on_origin(self, env):
        run(env, "up")
        new = push_commit(env, "third")
        p = run(env, "up")
        assert git(env["staging"], "rev-parse", "--short", "HEAD") == new
        assert "third" in p.stdout
        assert calls(env)[-1].startswith(f"restart {new} ")

    def test_no_frontend_is_fine(self, env):
        # 이 fixture 엔 frontend/ 가 없다 — npm ci 단계를 조용히 건너뛰어야 한다
        p = run(env, "up")
        assert "npm ci" not in p.stdout

    def test_refuses_without_folder_or_marker(self, env):
        p = run(env, "up", check=False, staging_dir=env["tmp"] / "nope")
        assert p.returncode != 0 and "setup-worktrees.sh" in p.stderr
        (env["staging"] / ".deploy-worktree").write_text("prod\n")   # STAGING_DIR 가 운영 폴더를 가리킴
        p = run(env, "down", check=False)
        assert p.returncode != 0 and "staging 폴더가 아닙니다" in p.stderr
        (env["staging"] / ".deploy-worktree").unlink()
        p = run(env, "up", check=False)
        assert p.returncode != 0 and "마커" in p.stderr
        assert calls(env) == []


class TestDownAndStatus:
    def test_down_calls_stop_only(self, env):
        run(env, "down")
        assert calls(env) == ["stop"]

    def test_status_reports_behind_and_health(self, env):
        run(env, "up")
        p = run(env, "status")
        assert "origin/main 와 같음" in p.stdout
        assert "[stub] ok :8010/:3010" in p.stdout
        push_commit(env, "third")
        p = run(env, "status")
        assert "1 커밋 뒤" in p.stdout and "staging.sh up" in p.stdout

    def test_help_and_bad_command(self, env):
        assert "staging.sh" in run(env).stdout
        assert run(env, "sideways", check=False).returncode == 2
