"""Offline unit tests for the interactive launcher (debug/__main__.py).

No Langfuse credentials, no network: the dispatch point (_run) is monkeypatched
so no child process ever starts, except the final end-to-end smoke which only
opens the menu and quits.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import debug.__main__ as launcher

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _feed(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Make launcher._ask return the given lines in order (EOFError after)."""
    it = iter(lines)

    def fake_ask(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(launcher, "_ask", fake_ask)


class _Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.verbose_flags: list[bool] = []
        self.returncode = returncode

    def __call__(self, tool: str, args: list[str], *, verbose: bool = True) -> int:
        self.calls.append((tool, args))
        self.verbose_flags.append(verbose)
        return self.returncode


# ── argv builders ────────────────────────────────────────────────────────────


def test_pipeline_accepts_8hex_tid(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "a687e093", "n")
    assert launcher._args_pipeline() == ["a687e093"]


def test_pipeline_accepts_32hex_and_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    tid32 = "51c47a5061f70aa291ce68a70f9407e3"
    _feed(monkeypatch, tid32, "y")
    assert launcher._args_pipeline() == [tid32, "--raw"]


def test_pipeline_accepts_16hex_langfuse_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known production Langfuse ID format (12~40 hex) — resolve_tid() parity."""
    _feed(monkeypatch, "51c47a5061f70aa2", "n")
    assert launcher._args_pipeline() == ["51c47a5061f70aa2"]


def test_bad_tid_reprompts_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "zzz", "A687E093", "n")  # invalid, then upper (lowered)
    assert launcher._args_pipeline() == ["a687e093"]


def test_empty_tid_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "")
    assert launcher._args_pipeline() is None


def test_logs_rejects_32hex(monkeypatch: pytest.MonkeyPatch) -> None:
    tid32 = "51c47a5061f70aa291ce68a70f9407e3"
    _feed(monkeypatch, tid32, "a687e093", "")
    assert launcher._args_logs() == ["a687e093"]


def test_logs_with_log_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "a687e093", "/mnt/server/logs")
    assert launcher._args_logs() == ["a687e093", "--log-dir", "/mnt/server/logs"]


def test_session_freeform_and_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "217ac056-aaaa-bbbb-cccc-121212121212")
    assert launcher._args_session() == ["217ac056-aaaa-bbbb-cccc-121212121212"]
    _feed(monkeypatch, "")
    assert launcher._args_session() is None


def test_status_default_and_custom_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "")
    assert launcher._args_status() == []
    _feed(monkeypatch, "http://10.0.0.5:8000")
    assert launcher._args_status() == ["--server-url", "http://10.0.0.5:8000"]


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (("1",), []),
        (("2", "rewrite_query"), ["--node", "rewrite_query"]),
        (("3",), ["--list-nodes"]),
        (("4",), ["--errors"]),
        (("5", "50"), ["--last", "50"]),
        (("b",), None),
        (("5", "abc", "50"), ["--last", "50"]),  # non-numeric N re-prompts
        (("5", "abc", ""), None),  # …and empty backs out
    ],
)
def test_analyze_submenu(
    monkeypatch: pytest.MonkeyPatch, inputs: tuple[str, ...], expected: list[str] | None
) -> None:
    _feed(monkeypatch, *inputs)
    assert launcher._args_analyze() == expected


def test_repro_search_builds_args_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, "2", "졸업학점", "0.5")
    assert launcher._args_repro() == ["search", "졸업학점", "--threshold", "0.5"]
    assert "운영박스 전용" in capsys.readouterr().out


def test_repro_threshold_reprompts_on_non_float(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd threshold must not reach the child (argparse exit 2 would be
    mislabeled as missing prod deps)."""
    _feed(monkeypatch, "2", "졸업학점", "0,5", "0.5")
    assert launcher._args_repro() == ["search", "졸업학점", "--threshold", "0.5"]


def test_repro_threshold_empty_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "2", "졸업학점", "0,5", "")
    assert launcher._args_repro() == ["search", "졸업학점"]


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (("1", "언제 수강신청?"), ["rewrite", "언제 수강신청?"]),
        (("3", "수강편람.md"), ["chunk", "수강편람.md"]),
        (("4", "안내_parent_0"), ["parent", "안내_parent_0"]),
        (("5", "기말고사 언제?"), ["answer", "기말고사 언제?"]),
        (("b",), None),
        (("1", ""), None),  # empty question backs out
    ],
)
def test_repro_submenu(
    monkeypatch: pytest.MonkeyPatch, inputs: tuple[str, ...], expected: list[str] | None
) -> None:
    _feed(monkeypatch, *inputs)
    assert launcher._args_repro() == expected


# ── menu loop & dispatch ─────────────────────────────────────────────────────


def test_menu_dispatches_then_quits(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    _feed(monkeypatch, "5", "a687e093", "", "q")
    assert launcher.main([]) == 0
    assert rec.calls == [("logs", ["a687e093"])]


def test_menu_survives_nonzero_child(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(returncode=2)
    monkeypatch.setattr(launcher, "_run", rec)
    _feed(monkeypatch, "1", "", "1", "", "q")
    assert launcher.main([]) == 0
    assert rec.calls == [("status", []), ("status", [])]


def test_invalid_choice_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    _feed(monkeypatch, "9", "x", "q")
    assert launcher.main([]) == 0
    assert rec.calls == []


def test_eof_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    _feed(monkeypatch)  # immediate EOF
    assert launcher.main([]) == 0
    assert rec.calls == []


def test_builder_back_returns_to_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    _feed(monkeypatch, "3", "", "q")  # pipeline → empty tid → back → quit
    assert launcher.main([]) == 0
    assert rec.calls == []


def test_ctrl_c_returns_to_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """KeyboardInterrupt at any prompt goes back to the menu, not out of the launcher."""
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    answers = iter([KeyboardInterrupt, "5", "a687e093", "", "q"])

    def fake_ask(prompt: str) -> str:
        item = next(answers)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        return item  # type: ignore[return-value]

    monkeypatch.setattr(launcher, "_ask", fake_ask)
    assert launcher.main([]) == 0
    assert rec.calls == [("logs", ["a687e093"])]


def test_run_quotes_spaced_args(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The echoed command must be copy-paste safe for questions with spaces."""
    monkeypatch.setattr(
        launcher.subprocess, "run",
        lambda cmd, **kw: type("P", (), {"returncode": 0})(),
    )
    launcher._run("repro", ["rewrite", "언제 수강신청?"])
    out = capsys.readouterr().out
    assert "'언제 수강신청?'" in out


# ── passthrough mode ─────────────────────────────────────────────────────────


def test_passthrough_known_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(returncode=7)
    monkeypatch.setattr(launcher, "_run", rec)
    assert launcher.main(["logs", "a687e093"]) == 7
    assert rec.calls == [("logs", ["a687e093"])]
    assert rec.verbose_flags == [False]  # pipe-safe: no header/footer added


def test_passthrough_adds_nothing_to_output() -> None:
    """python -m debug logs X must produce byte-identical output to python -m debug.logs X."""
    kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                  cwd=_REPO_ROOT, timeout=60)
    direct = subprocess.run([sys.executable, "-m", "debug.logs", "a687e093"], **kwargs)
    passthru = subprocess.run([sys.executable, "-m", "debug", "logs", "a687e093"], **kwargs)
    assert passthru.returncode == direct.returncode
    assert passthru.stdout == direct.stdout


def test_passthrough_unknown_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    assert launcher.main(["bogus"]) == 2
    assert rec.calls == []
    assert "unknown tool" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(launcher, "_run", rec)
    assert launcher.main([flag]) == 0
    assert rec.calls == []
    assert "usage" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="death-by-signal exit is POSIX-only")
def test_passthrough_replays_child_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Child killed by signal N → launcher re-kills itself with N, so the shell
    sees the same status as a direct `python -m debug.<tool>` run."""
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        launcher.subprocess, "run",
        lambda cmd, **kw: type("P", (), {"returncode": -11})(),
    )
    monkeypatch.setattr(launcher.signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert launcher.main(["logs", "a687e093"]) == 139  # fallback if signal ignored
    assert killed == [(os.getpid(), 11)]


def test_menu_text_matches_dispatch_table() -> None:
    """Tripwire: the hand-aligned menu text and the _MENU dict can't drift."""
    rows = re.findall(r"^\s*(\d)\..*\(debug\.(\w+)\)", launcher._MENU_TEXT, re.M)
    assert dict(rows) == {num: tool for num, (tool, _) in launcher._MENU.items()}
    assert launcher._TOOLS == tuple(tool for tool, _ in launcher._MENU.values())


# ── end-to-end smoke (real subprocess, offline) ──────────────────────────────


def test_python_dash_m_debug_quits_cleanly() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "debug"],
        input="q\n",
        capture_output=True,
        text=True,
        encoding="utf-8",  # child reconfigures stdout to UTF-8; don't decode with locale (cp949)
        errors="replace",
        cwd=_REPO_ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "BUFS" in proc.stdout
