"""scripts/healthcheck.sh — `cron`(타이머 자동 복구 정책)과 `alert`(중복 억제).

점검과 재기동은 HC_CHECK_CMD / HC_RESTART_CMD 훅으로 스텁한다. CAMCHAT_UNITS_DIR 를 빈 값으로 정의해
systemd 유닛이 "이 폴더 것" 으로 잡히지 않게 한다 (_common.sh 의 테스트 훅) — 진짜 systemctl 은 안 부른다.
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
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


STUB_CHECK = """#!/usr/bin/env bash
root="$(cd "$(dirname "$0")" && pwd)"
rc="$(cat "$root/CHECK_RC" 2>/dev/null || echo 0)"
if [ "$rc" = 0 ]; then echo "[backend ] ok"; echo "ALL OK"; else echo "[backend ] DOWN"; fi
exit "$rc"
"""
STUB_RESTART = """#!/usr/bin/env bash
root="$(cd "$(dirname "$0")" && pwd)"
echo restart >>"$root/restart-calls"
"""


def _executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "_common.sh").write_bytes((SCRIPTS / "_common.sh").read_bytes())
    _executable(
        tmp_path / "scripts" / "healthcheck.sh",
        (SCRIPTS / "healthcheck.sh").read_text(encoding="utf-8"),
    )
    _executable(tmp_path / "stub-check.sh", STUB_CHECK)
    _executable(tmp_path / "stub-restart.sh", STUB_RESTART)
    return tmp_path


def run(root, *args, check_rc=0, env=None):
    (root / "CHECK_RC").write_text(f"{check_rc}\n")
    e = os.environ.copy()
    e["CAMCHAT_UNITS_DIR"] = ""
    e["HC_CHECK_CMD"] = f"bash {root / 'stub-check.sh'}"
    e["HC_RESTART_CMD"] = f"bash {root / 'stub-restart.sh'}"
    e.pop("ALERT_WEBHOOK_URL", None)
    e.update(env or {})
    return subprocess.run(
        ["bash", str(root / "scripts" / "healthcheck.sh"), *args],
        capture_output=True,
        text=True,
        env=e,
        cwd=root,
        stdin=subprocess.DEVNULL,
    )


def alerts(root):
    f = root / "logs" / "alerts.log"
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def restarts(root):
    f = root / "restart-calls"
    return f.read_text().splitlines() if f.exists() else []


def state(root):
    f = root / "logs" / "run" / "healthcheck.state"
    return dict(line.split("=", 1) for line in f.read_text().splitlines()) if f.exists() else {}


class TestCron:
    def test_ok_records_and_exits_zero(self, root):
        p = run(root, "cron")
        assert p.returncode == 0 and p.stdout.rstrip().endswith("ok")
        assert state(root)["fails"] == "0" and state(root)["status"] == "ok" and alerts(root) == []

    def test_first_failure_alerts_once_and_does_not_restart(self, root):
        p = run(root, "cron", check_rc=1)
        assert p.returncode == 1 and "FAIL #1" in p.stdout
        assert restarts(root) == []
        assert len(alerts(root)) == 1 and "1/2" in alerts(root)[0]

    def test_second_failure_restarts_then_grace_then_recovery_alert(self, root):
        run(root, "cron", check_rc=1)
        p = run(root, "cron", check_rc=1)
        assert p.returncode == 1 and "재기동함 (이번 시간 1/3)" in p.stdout
        assert restarts(root) == ["restart"]
        assert any("재기동" in a for a in alerts(root))
        # 재기동 직후는 유예 — 실패로 세지 않고 재기동도 다시 안 한다
        p = run(root, "cron", check_rc=1)
        assert "아직 뜨는 중" in p.stdout and restarts(root) == ["restart"]
        # 복구되면 "복구됨" 알림 한 번
        p = run(root, "cron")
        assert p.returncode == 0 and any("복구됨" in a for a in alerts(root))

    def test_maintenance_flag_skips_everything(self, root):
        (root / "logs" / "run").mkdir(parents=True)
        (root / "logs" / "run" / "maintenance").write_text("1 test\n")
        p = run(root, "cron", check_rc=1)
        assert (
            p.returncode == 0
            and "건너뜀" in p.stdout
            and restarts(root) == []
            and state(root) == {}
        )

    def test_restart_cap_suspends_with_one_alert(self, root):
        import time

        now = int(time.time())
        (root / "logs" / "run").mkdir(parents=True)
        (root / "logs" / "run" / "healthcheck.state").write_text(
            f'fails=1\nstatus=down\nrestarts="{now - 100} {now - 200} {now - 300}"\ncooldown_until=0\n'
        )
        p = run(root, "cron", check_rc=1)
        assert p.returncode == 1 and restarts(root) == []
        assert state(root)["status"] == "suspended" and any("중단" in a for a in alerts(root))
        run(root, "cron", check_rc=1)
        assert sum("중단" in a for a in alerts(root)) == 1  # 같은 상태를 반복 알리지 않는다

    def test_dry_run_does_not_restart(self, root):
        run(root, "cron", check_rc=1)
        p = run(root, "cron", check_rc=1, env={"HC_DRY_RUN": "1"})
        assert "DRY RUN" in p.stdout and restarts(root) == []


class TestAlert:
    def test_logs_and_dedupes_same_title(self, root):
        p1 = run(root, "alert", "제목", "본문")
        p2 = run(root, "alert", "제목", "본문 2")
        assert p1.returncode == 0 and p2.returncode == 0
        assert "ALERT_WEBHOOK_URL 없음" in p1.stdout and "같은 제목" in p2.stdout
        assert len(alerts(root)) == 2 and "제목 — 본문 2" in alerts(root)[1]

    def test_different_title_is_not_deduped(self, root):
        run(root, "alert", "하나", "")
        p = run(root, "alert", "둘", "")
        assert "같은 제목" not in p.stdout


class TestCli:
    def test_unknown_command_is_rejected(self, root):
        p = run(root, "bogus")
        assert p.returncode == 2 and "알 수 없는 명령" in p.stderr

    def test_help(self, root):
        p = run(root, "--help")
        assert p.returncode == 0 and "healthcheck.sh cron" in p.stdout

    def test_malformed_dedupe_state_does_not_break_alerting(self, root):
        """logs/run/alert.last 의 2번째 줄이 숫자가 아니면(잘린 파일) 산술 오류 없이 그냥 보낸다."""
        (root / "logs" / "run").mkdir(parents=True)
        (root / "logs" / "run" / "alert.last").write_text("제목\n\n")
        p = run(root, "alert", "제목", "본문")
        assert p.returncode == 0 and "syntax error" not in p.stderr and "같은 제목" not in p.stdout
        assert len(alerts(root)) == 1


class TestCheck:
    def test_non_json_health_body_is_down(self, root):
        """200 인데 JSON 이 아닌 /health (터널·프록시가 대신 답함) 는 정상이 아니다."""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>tunnel</html>")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            e = os.environ.copy()
            e["BACKEND_PORT"] = str(srv.server_address[1])
            e["FRONTEND_PORT"] = str(srv.server_address[1])
            p = subprocess.run(
                ["bash", str(root / "scripts" / "healthcheck.sh")],
                capture_output=True,
                text=True,
                env=e,
                cwd=root,
            )
            assert p.returncode == 1 and "[backend ] DOWN" in p.stdout and "ALL OK" not in p.stdout
        finally:
            srv.shutdown()

    def test_caller_ports_win_over_env_local(self, root):
        """호출자가 준 BACKEND_PORT 가 scripts/env.local 의 값보다 우선한다 (staging.sh status 가 다른 폴더를 찍을 때)."""
        (root / "scripts" / "env.local").write_text(
            "export BACKEND_PORT=1\nexport FRONTEND_PORT=1\n"
        )
        port = _free_port()
        e = os.environ.copy()
        e["BACKEND_PORT"] = str(port)
        e["FRONTEND_PORT"] = str(port)
        p = subprocess.run(
            ["bash", str(root / "scripts" / "healthcheck.sh")],
            capture_output=True,
            text=True,
            env=e,
            cwd=root,
        )
        # 그 포트엔 아무것도 없다 — :1 이 아니라 호출자의 포트를 봤다는 뜻
        assert p.returncode == 1 and "[backend ] DOWN" in p.stdout
