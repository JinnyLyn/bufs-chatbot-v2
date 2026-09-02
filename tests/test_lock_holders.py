"""scripts/_common.sh 의 lock_holders — 잠금 파일을 '지금 잡고 있는' 프로세스 찾기.

내장 Qdrant 는 단일 writer 라 재색인 중에 백엔드를 띄우면 인덱스를 못 연다. 그런데
잠금 파일 자체는 소유 프로세스가 죽어도 디스크에 남기 때문에, 파일 존재만 보고 경고하면
재시작할 때마다(중지와 시작 사이) 헛경고가 뜬다 — 실제로 매번 떴다. 파일이 아니라
'열고 있는 프로세스'를 봐야 한다.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

COMMON = Path(__file__).resolve().parent.parent / "scripts" / "_common.sh"


def _run(snippet: str, cwd: Path) -> subprocess.CompletedProcess:
    """_common.sh 를 읽어들인 뒤 스니펫을 실행한다."""
    script = f'. "{COMMON}" >/dev/null 2>&1\n{textwrap.dedent(snippet)}'
    return subprocess.run(["bash", "-c", script], cwd=cwd,
                          capture_output=True, text=True)


def test_no_holder_for_a_plain_file(tmp_path: Path):
    """아무도 안 잡은 파일 → 빈 출력. 재시작 사이의 잔여 파일이 이 경우다."""
    lock = tmp_path / ".lock"
    lock.write_text("tmp lock file", encoding="utf-8")
    p = _run(f'lock_holders "{lock}"', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == "", f"잡은 프로세스가 없는데 PID 를 보고했다: {p.stdout!r}"


def test_missing_file_is_not_an_error(tmp_path: Path):
    p = _run(f'lock_holders "{tmp_path / "없는파일"}"', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_reports_the_pid_that_holds_the_file_open(tmp_path: Path):
    """열려 있는 동안에는 그 PID 를 보고해야 한다 — 진짜 경고를 내야 하는 상황이다."""
    lock = tmp_path / ".lock"
    lock.write_text("x", encoding="utf-8")
    # 파일을 연 채로 대기하는 자식 프로세스를 만들고, 그동안 조회한다.
    p = _run(f'''
        exec 9<"{lock}"          # 이 셸이 파일을 잡는다
        lock_holders "{lock}"
        exec 9<&-
    ''', tmp_path)
    assert p.returncode == 0
    pids = [line for line in p.stdout.split() if line.isdigit()]
    assert pids, f"열려 있는 파일의 보유 PID 를 못 찾았다: {p.stdout!r}"


def test_released_after_the_holder_closes(tmp_path: Path):
    """닫은 뒤에는 다시 빈 출력 — 파일이 남아 있어도 경고하지 않아야 한다."""
    lock = tmp_path / ".lock"
    lock.write_text("x", encoding="utf-8")
    p = _run(f'''
        exec 9<"{lock}"
        exec 9<&-
        lock_holders "{lock}"
    ''', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == ""
    assert lock.exists(), "파일 자체는 남아 있는 상황을 재현해야 의미가 있다"
