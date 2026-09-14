"""scripts/_common.sh 의 units_refresh — 설치된 systemd 유닛을 체크아웃의 scripts/systemd/* 로 맞추기.

진짜 systemd 는 건드리지 않는다: CAMCHAT_UNITS_DIR(유닛이 서비스하는 폴더 흉내) + CAMCHAT_UNIT_DIR(샌드박스
설치 폴더) 를 같이 주고, PATH 앞에 가짜 systemctl / systemd-analyze 를 둔다 (호출 기록만 남김; 검증 실패는
STUB_VERIFY_RC 로 흉내).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >>"$STUB_LOG"
exit 0
"""
FAKE_ANALYZE = """#!/usr/bin/env bash
echo "systemd-analyze $*" >>"$STUB_LOG"
exit "${STUB_VERIFY_RC:-0}"
"""


def _executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts" / "systemd").mkdir(parents=True)
    (r / "scripts" / "_common.sh").write_bytes((SCRIPTS / "_common.sh").read_bytes())
    for f in (SCRIPTS / "systemd").iterdir():
        (r / "scripts" / "systemd" / f.name).write_bytes(f.read_bytes())
    (r / "scripts" / "logrotate.conf").write_bytes((SCRIPTS / "logrotate.conf").read_bytes())
    bins = tmp_path / "bin"
    bins.mkdir()
    _executable(bins / "systemctl", FAKE_SYSTEMCTL)
    _executable(bins / "systemd-analyze", FAKE_ANALYZE)
    (tmp_path / "units").mkdir()
    return r


def refresh(repo, verify_rc=0):
    tmp = repo.parent
    e = os.environ.copy()
    e["PATH"] = f"{tmp / 'bin'}:{e['PATH']}"
    e["CAMCHAT_UNITS_DIR"] = str(repo)  # 유닛이 이 폴더를 서비스한다고 친다
    e["CAMCHAT_UNIT_DIR"] = str(tmp / "units")  # 설치본은 여기
    e["STUB_LOG"] = str(tmp / "calls")
    e["STUB_VERIFY_RC"] = str(verify_rc)
    p = subprocess.run(
        ["bash", "-c", f'. "{repo}/scripts/_common.sh"; units_refresh; echo "rc=$?"'],
        capture_output=True,
        text=True,
        env=e,
        cwd=repo,
    )
    rc = int(p.stdout.strip().splitlines()[-1].split("=")[1])
    return rc, p


def calls(repo):
    f = repo.parent / "calls"
    return f.read_text().splitlines() if f.exists() else []


def installed(repo):
    return sorted(p.name for p in (repo.parent / "units").iterdir())


class TestUnitsRefresh:
    def test_first_install_renders_verifies_and_reloads(self, repo):
        rc, p = refresh(repo)
        assert rc == 0, p.stderr
        assert installed(repo) == sorted(f.name for f in (repo / "scripts" / "systemd").iterdir())
        text = (repo.parent / "units" / "camchat-backend.service").read_text()
        assert f"WorkingDirectory={repo}" in text and "%h/camchat" not in text
        assert any(c.startswith("systemd-analyze --user verify") for c in calls(repo))
        assert calls(repo)[-1] == "systemctl --user daemon-reload"
        assert (repo / "logs" / "run" / "logrotate.conf").exists()

    def test_no_change_is_a_noop(self, repo):
        refresh(repo)
        (repo.parent / "calls").unlink()
        rc, _ = refresh(repo)
        assert rc == 1 and calls(repo) == []

    def test_changed_unit_is_reinstalled_with_backup(self, repo):
        refresh(repo)
        unit = repo / "scripts" / "systemd" / "camchat-backend.service"
        unit.write_text(unit.read_text().replace("RestartSec=15", "RestartSec=20"))
        rc, _ = refresh(repo)
        assert rc == 0
        assert "RestartSec=20" in (repo.parent / "units" / "camchat-backend.service").read_text()
        assert (
            "RestartSec=15"
            in (repo / "logs" / "run" / "units-prev" / "camchat-backend.service").read_text()
        )

    def test_verify_failure_changes_nothing(self, repo):
        refresh(repo)
        before = {p.name: p.read_text() for p in (repo.parent / "units").iterdir()}
        unit = repo / "scripts" / "systemd" / "camchat-backend.service"
        unit.write_text(unit.read_text() + "Bogus=1\n")
        (repo.parent / "calls").unlink()
        rc, p = refresh(repo, verify_rc=1)
        assert rc == 2 and "검증에 실패" in p.stderr
        assert {q.name: q.read_text() for q in (repo.parent / "units").iterdir()} == before
        assert not any("daemon-reload" in c for c in calls(repo))

    def test_unit_removed_from_tree_is_disabled_and_deleted(self, repo):
        refresh(repo)
        (repo / "scripts" / "systemd" / "camchat-logrotate.timer").unlink()
        (repo / "scripts" / "systemd" / "camchat-logrotate.service").unlink()
        rc, _ = refresh(repo)
        assert rc == 0
        assert "camchat-logrotate.timer" not in installed(repo)
        assert "systemctl --user disable --now camchat-logrotate.timer" in calls(repo)

    def test_units_dir_hook_alone_never_touches_systemd(self, repo):
        """CAMCHAT_UNITS_DIR 만 있고 CAMCHAT_UNIT_DIR 가 없으면(다른 테스트들의 설정) 아무것도 안 한다."""
        e = os.environ.copy()
        e["PATH"] = f"{repo.parent / 'bin'}:{e['PATH']}"
        e["CAMCHAT_UNITS_DIR"] = str(repo)
        e.pop("CAMCHAT_UNIT_DIR", None)
        e["STUB_LOG"] = str(repo.parent / "calls")
        p = subprocess.run(
            ["bash", "-c", f'. "{repo}/scripts/_common.sh"; units_refresh; echo "rc=$?"'],
            capture_output=True,
            text=True,
            env=e,
            cwd=repo,
        )
        assert p.stdout.strip().endswith("rc=1") and calls(repo) == [] and installed(repo) == []

    def test_install_failure_restores_previous_and_returns_3(self, repo):
        """설치본에 쓸 수 없으면 3 — 설치본은 그대로, "갱신했다" 고 말하지 않는다."""
        refresh(repo)
        before = {p.name: p.read_text() for p in (repo.parent / "units").iterdir()}
        unit = repo / "scripts" / "systemd" / "camchat-backend.service"
        unit.write_text(unit.read_text().replace("RestartSec=15", "RestartSec=20"))
        target = repo.parent / "units" / "camchat-backend.service"
        target.chmod(0o444)  # 읽기 전용 파일: cp 가 열지 못한다 (root 가 아닐 때)
        try:
            rc, p = refresh(repo)
        finally:
            target.chmod(0o644)
        assert rc == 3 and "설치 실패" in p.stderr and "갱신했습니다" not in p.stdout, (
            p.stdout,
            p.stderr,
        )
        assert {q.name: q.read_text() for q in (repo.parent / "units").iterdir()} == before
