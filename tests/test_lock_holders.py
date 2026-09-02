"""scripts/_common.sh 의 lock_holders — 잠금 파일을 '실제로 잠근' 프로세스 찾기.

내장 Qdrant 는 단일 writer 라 재색인 중에 백엔드를 띄우면 인덱스를 못 연다. 그런데
잠금 파일 자체는 소유 프로세스가 죽어도 디스크에 남기 때문에, 파일 존재만 보고 경고하면
재시작할 때마다(중지와 시작 사이) 헛경고가 뜬다 — 실제로 매번 떴다.

파일을 '열고 있는지' 로도 부족하다. 백업이나 진단이 파일을 잠깐 열기만 해도(예: 인덱스
디렉터리 복사) 보유자로 잡혀 같은 오탐이 되살아난다. 그래서 커널 잠금 테이블
(/proc/locks)을 보고 advisory lock 보유 여부로 판정한다.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

COMMON = Path(__file__).resolve().parent.parent / "scripts" / "_common.sh"
needs_flock = pytest.mark.skipif(shutil.which("flock") is None,
                                 reason="flock(1) 없음 — 잠금 보유 케이스 검증 불가")


def _run(snippet: str, cwd: Path) -> subprocess.CompletedProcess:
    """_common.sh 를 읽어들인 뒤 스니펫을 실행한다.

    경로는 반드시 소싱 '뒤에' 정한다 — _common.sh 가 REPO 를 저장소 루트로 덮어쓰기
    때문에, 먼저 정하면 임시 파일 대신 실제 인덱스 잠금을 조회하게 된다.
    """
    script = f'. "{COMMON}" >/dev/null 2>&1\n{textwrap.dedent(snippet)}'
    return subprocess.run(["bash", "-c", script], cwd=cwd,
                          capture_output=True, text=True)


def _lock_file(tmp_path: Path) -> Path:
    lock = tmp_path / ".lock"
    lock.write_text("tmp lock file", encoding="utf-8")
    return lock


def test_no_holder_for_an_untouched_file(tmp_path: Path):
    """아무도 안 잡은 파일 → 빈 출력. 재시작 사이의 잔여 파일이 이 경우다."""
    lock = _lock_file(tmp_path)
    p = _run(f'lock_holders "{lock}"', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == "", f"보유자가 없는데 PID 를 보고했다: {p.stdout!r}"


def test_missing_file_is_not_an_error(tmp_path: Path):
    p = _run(f'lock_holders "{tmp_path / "없는파일"}"', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_open_without_lock_is_not_a_holder(tmp_path: Path):
    """열기만 한 프로세스는 보유자가 아니다 — 백업·진단이 경고를 띄우면 안 된다."""
    lock = _lock_file(tmp_path)
    p = _run(f'''
        exec 9<"{lock}"          # 열되 잠그지는 않는다
        lock_holders "{lock}"
        exec 9<&-
    ''', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == "", (
        f"파일을 열기만 한 프로세스를 잠금 보유자로 보고했다: {p.stdout!r}")


@needs_flock
def test_flock_holder_is_reported(tmp_path: Path):
    """실제로 advisory lock 을 잡은 프로세스는 PID 로 보고돼야 한다 — 진짜 경고 상황."""
    lock = _lock_file(tmp_path)
    p = _run(f'''
        exec 9<"{lock}"
        flock -x 9               # 여기서부터 진짜 보유자
        lock_holders "{lock}"
        exec 9<&-
    ''', tmp_path)
    assert p.returncode == 0
    pids = [line for line in p.stdout.split() if line.isdigit()]
    assert pids, f"잠금을 잡고 있는데 보유자를 못 찾았다: {p.stdout!r}"


@needs_flock
def test_released_after_the_holder_exits(tmp_path: Path):
    """잠금이 풀린 뒤에는 파일이 남아 있어도 조용해야 한다 — 이번 수정의 핵심."""
    lock = _lock_file(tmp_path)
    p = _run(f'''
        ( exec 9<"{lock}"; flock -x 9; ) # 하위 셸이 끝나면서 잠금 해제
        lock_holders "{lock}"
    ''', tmp_path)
    assert p.returncode == 0
    assert p.stdout.strip() == ""
    assert lock.exists(), "파일은 남아 있는 상황을 재현해야 의미가 있다"
