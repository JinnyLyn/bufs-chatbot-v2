"""scripts/stack.sh — 인자 검사와, 스택이 없는 폴더에서의 stop/run 안전 경로.

진짜 스택은 띄우지 않는다: 포트는 쓰지 않는 값으로 두고, CAMCHAT_UNITS_DIR 를 빈 값으로 정의해 systemd
유닛이 "이 폴더 것" 으로 잡히지 않게 한다 (_common.sh 의 테스트 훅). stop 은 이 체크아웃에 묶인 프로세스가
없으니 아무것도 죽이지 않고 "nothing to stop" 으로 끝나야 한다.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _free_port():
    """127.0.0.1 은 이 공유 서버의 모두가 쓴다 — 고정 포트는 남의 리스너와 겹칠 수 있으니 매번 빈 포트를 받는다."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "_common.sh").write_bytes((SCRIPTS / "_common.sh").read_bytes())
    stack = tmp_path / "scripts" / "stack.sh"
    stack.write_text((SCRIPTS / "stack.sh").read_text(encoding="utf-8"), encoding="utf-8")
    stack.chmod(stack.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def run(root, *args, env=None):
    e = os.environ.copy()
    e["CAMCHAT_UNITS_DIR"] = ""
    e["BACKEND_PORT"] = str(_free_port())
    e["FRONTEND_PORT"] = str(_free_port())
    e["OLLAMA_PORT"] = str(_free_port())
    e.pop("CUDA_VISIBLE_DEVICES", None)
    e.update(env or {})
    return subprocess.run(
        ["bash", str(root / "scripts" / "stack.sh"), *args],
        capture_output=True,
        text=True,
        env=e,
        cwd=root,
        stdin=subprocess.DEVNULL,
    )


class TestCli:
    def test_no_command_prints_usage_and_fails(self, root):
        p = run(root)
        assert p.returncode == 2 and "stack.sh start" in p.stderr

    def test_help(self, root):
        p = run(root, "--help")
        assert p.returncode == 0 and "stack.sh run backend|frontend|ollama" in p.stdout

    @pytest.mark.parametrize(
        "args",
        [
            ("bogus",),
            ("stop", "--bogus"),
            ("restart", "--bogus"),
            ("start", "extra"),
            ("run",),
            ("run", "bogus"),
        ],
    )
    def test_bad_arguments_are_rejected_before_acting(self, root, args):
        p = run(root, *args)
        assert p.returncode == 2, (args, p.stdout, p.stderr)


class TestStop:
    def test_nothing_to_stop_in_an_empty_checkout(self, root):
        p = run(root, "stop")
        assert p.returncode == 0, p.stderr
        assert "frontend: 내릴 것 없음" in p.stdout and "backend: 내릴 것 없음" in p.stdout
        assert "Ollama 는 그대로 둠" in p.stdout

    def test_with_ollama_is_ignored_when_this_folder_borrows_ollama(self, root):
        p = run(root, "stop", "--with-ollama", env={"START_OLLAMA": "no"})
        assert p.returncode == 0 and "--with-ollama 무시" in p.stdout


class TestRun:
    def test_backend_refuses_interpreter_without_fastapi(self, root):
        p = run(root, "run", "backend", env={"PYTHON": "/bin/false"})
        assert p.returncode == 1 and "cannot import fastapi" in p.stderr

    def test_frontend_refuses_without_standalone_build(self, root):
        p = run(root, "run", "frontend")
        assert p.returncode == 1 and "standalone 빌드가 없습니다" in p.stderr

    def test_ollama_refuses_without_gpu_pin(self, root):
        p = run(root, "run", "ollama")
        assert p.returncode == 1 and "CUDA_VISIBLE_DEVICES" in p.stderr

    def test_backend_started_with_interpreter_flags_is_still_ours(self, root):
        """`python -u project/server.py` 처럼 플래그가 끼어도 이 체크아웃의 백엔드로 보고 내린다 (옛 글롭이 잡던 형태)."""
        import time

        (root / "project").mkdir()
        (root / "project" / "server.py").write_text("import time; time.sleep(30)\n")
        fake = subprocess.Popen(
            ["python3", "-u", "-X", "utf8", "project/server.py"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.2)
            p = run(root, "stop")
            assert "pidfile 없이 떠 있는 이 체크아웃의 backend" in p.stdout, p.stdout
            assert fake.wait(timeout=15) != 0
        finally:
            fake.kill()
            fake.wait()

    def test_python_that_merely_mentions_the_script_is_not_ours(self, root):
        """`python -c '… project/server.py'` 나 `python -m ruff check project/server.py` 는 백엔드가 아니다."""
        import time

        fake = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)  # project/server.py"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.2)
            p = run(root, "stop")
            assert p.returncode == 0 and "pidfile 없이 떠 있는" not in p.stdout
            assert fake.poll() is None, "stop killed a python -c that only mentioned server.py"
        finally:
            fake.kill()
            fake.wait()

    def test_shell_mentioning_the_backend_is_not_ours(self, root):
        """명령줄 텍스트에 python 과 project/server.py 가 같이 있는 셸(예: bash -c '…')은 백엔드가 아니다 —
        2026-09-14 에 stop 이 운영자 자신의 셸을 TERM 한 회귀."""
        import time

        bystander = subprocess.Popen(
            ["bash", "-c", "sleep 30 # python project/server.py"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.2)
            p = run(root, "stop")
            assert p.returncode == 0 and "pidfile 없이 떠 있는" not in p.stdout
            assert bystander.poll() is None, "stop killed a shell that merely mentioned server.py"
        finally:
            bystander.kill()
            bystander.wait()


class TestRestart:
    def test_preflight_failure_stops_nothing(self, root):
        """restart 의 사전 점검이 실패하면(여기선 venv 없음) 3 으로 끝나고, 떠 있던 백엔드는 건드리지 않는다."""
        import time

        (root / "project").mkdir()
        (root / "project" / "server.py").write_text("import time; time.sleep(30)\n")
        backend = subprocess.Popen(
            ["python3", "project/server.py"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.2)
            p = run(root, "restart", "--no-build", env={"PYTHON": "/bin/false"})
            assert p.returncode == 3 and "사전 점검 실패" in p.stderr, (p.stdout, p.stderr)
            assert backend.poll() is None, "preflight failure must not stop the running stack"
            assert not (root / "logs" / "run" / "maintenance").exists()
        finally:
            backend.kill()
            backend.wait()
