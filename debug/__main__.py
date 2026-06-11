"""Interactive launcher for the debug/ toolkit — `python -m debug`.

Numbered Korean menu over the six CLIs (status/analyze/pipeline/session/
logs/repro): pick a number, answer the prompts, and the equivalent
`python -m debug.<tool> …` command is printed and executed.

Passthrough: `python -m debug <tool> [args…]` ≡ `python -m debug.<tool> [args…]`.

Dispatch is via subprocess, not in-process import, on purpose:
- this module stays import-pure (imports NO debug submodule, mutates no env),
- each tool keeps its own env bootstrap and exact exit code,
- Ctrl-C during a long child run kills the child only; the menu survives,
- repro.main() takes no argv and sys.exit()s inside handlers — subprocess
  treats all six tools uniformly.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable

# Duplicated on purpose: importing debug.session/_query would break this
# module's import purity (no env bootstrap from the menu process).
# Keep in sync with _query.py resolve_tid(): 8-hex app tid, or a Langfuse
# trace ID of 12~40 hex (확인된 운영 형식 16-hex; 웹 UI는 32-hex일 수도).
_HEX8 = re.compile(r"^[0-9a-f]{8}$")
_HEX_LANGFUSE = re.compile(r"^[0-9a-f]{12,40}$")

# Per-tool exit-code meanings shown after a run (fallback: generic message).
# Source: each tool's --help / debug/README.md §3 표. argparse도 인자 오류에
# 2를 쓰므로 2번 라벨은 두 원인을 함께 적는다.
_EXIT_MEANINGS: dict[str, dict[int, str]] = {
    "status": {0: "정상", 1: "이상 감지", 2: "설정 오류 (project/.env 확인) 또는 잘못된 인자"},
    "analyze": {0: "정상", 1: "키 누락·조회 실패"},
    "session": {0: "정상", 1: "키 누락·조회 실패"},
    "pipeline": {0: "정상", 1: "키 누락·조회 실패"},
    "logs": {0: "정상", 2: "tid·인자 형식 오류"},
    "repro": {
        0: "정상",
        1: "환경변수·의존성 누락 / 실행 실패",
        2: "운영박스 전용 의존성 누락(search/answer) 또는 잘못된 인자",
    },
}


def _ask(prompt: str) -> str:
    """input() wrapper — single patch point for tests. Strips whitespace."""
    return input(prompt).strip()


def _ask_tid(prompt: str, *, allow_langfuse: bool = True) -> str | None:
    """Prompt for a trace id until valid. Empty or 'b' → None (back to menu)."""
    while True:
        raw = _ask(prompt).lower()
        if raw in ("", "b"):
            return None
        if _HEX8.match(raw) or (allow_langfuse and _HEX_LANGFUSE.match(raw)):
            return raw
        forms = "8자리 (예: a687e093)" + (" 또는 Langfuse ID 12~40자리" if allow_langfuse else "")
        print(f"  ✗ tid는 소문자 hex {forms}여야 합니다. 빈 입력=뒤로")


def _yes(prompt: str) -> bool:
    return _ask(prompt).lower() in ("y", "yes")


def _args_status() -> list[str] | None:
    url = _ask("서버 URL (엔터=기본 $BUFS_SERVER_URL 또는 http://localhost:8000): ")
    return ["--server-url", url] if url else []


def _args_analyze() -> list[str] | None:
    print(
        "  1. 전체 통계 (최근 7일)\n"
        "  2. 노드별 실행 이력\n"
        "  3. 노드 이름 목록 보기\n"
        "  4. 에러 관측만 보기\n"
        "  5. 최근 N건만\n"
        "  b. 뒤로"
    )
    choice = _ask("선택: ")
    if choice == "1":
        return []
    if choice == "2":
        node = _ask("노드 이름 (모르면 뒤로 가서 3번으로 목록 확인): ")
        return ["--node", node] if node else None
    if choice == "3":
        return ["--list-nodes"]
    if choice == "4":
        return ["--errors"]
    if choice == "5":
        while True:
            n = _ask("트레이스 개수 N: ")
            if n in ("", "b"):
                return None
            if n.isdigit():
                return ["--last", n]
            print("  ✗ 숫자를 입력하세요. 빈 입력=뒤로")
    return None


def _args_pipeline() -> list[str] | None:
    tid = _ask_tid("트레이스 ID (8-hex tid 또는 12~40-hex Langfuse ID): ")
    if tid is None:
        return None
    args = [tid]
    if _yes("주석 없이 raw 타임라인으로 볼까요? [y/N]: "):
        args.append("--raw")
    return args


def _args_session() -> list[str] | None:
    sid = _ask("세션 UUID 또는 8-hex tid (빈 입력=뒤로): ")
    return [sid] if sid else None


def _args_logs() -> list[str] | None:
    tid = _ask_tid("8-hex tid (예: a687e093): ", allow_langfuse=False)
    if tid is None:
        return None
    args = [tid]
    log_dir = _ask("로그 폴더 (엔터=기본 logs/ 또는 $BUFS_LOG_DIR): ")
    if log_dir:
        args += ["--log-dir", log_dir]
    return args


def _args_repro() -> list[str] | None:
    print(
        "  1. rewrite — 질의 재작성 재실행 (OLLAMA_BASE_URL 필요)\n"
        "  2. search  — 운영 검색 경로 재실행 ⚠ 운영박스 전용 (torch 필요)\n"
        "  3. chunk   — 마크다운 청킹 확인 (어디서나)\n"
        "  4. parent  — 부모 청크 조회 (어디서나)\n"
        "  5. answer  — e2e 답변 1회 ⚠ 운영박스 전용, 비결정적\n"
        "  b. 뒤로"
    )
    choice = _ask("선택: ")
    if choice == "1":
        q = _ask("재작성할 사용자 질문: ")
        return ["rewrite", q] if q else None
    if choice == "2":
        print("  ⚠ 운영박스 전용: dev/WSL에서는 의존성 안내와 함께 종료됩니다 (exit 2)")
        q = _ask("검색 쿼리: ")
        if not q:
            return None
        args = ["search", q]
        while True:
            thr = _ask("관련성 게이트 임계값 (엔터=운영 기본값): ")
            if not thr:
                break
            try:
                float(thr)
            except ValueError:
                print("  ✗ 숫자여야 합니다 (예: 0.3). 엔터=기본값")
                continue
            args += ["--threshold", thr]
            break
        return args
    if choice == "3":
        f = _ask("마크다운 파일 경로 (또는 markdown_docs/ 안의 파일명): ")
        return ["chunk", f] if f else None
    if choice == "4":
        pid = _ask("parent_id (예: 2026학년도1학기학사안내_parent_0): ")
        return ["parent", pid] if pid else None
    if choice == "5":
        print("  ⚠ 운영박스 전용 + 비결정적: 폭주 사례가 그대로 재현되지 않을 수 있습니다")
        q = _ask("사용자 질문: ")
        return ["answer", q] if q else None
    return None


_MENU: dict[str, tuple[str, Callable[[], "list[str] | None"]]] = {
    "1": ("status", _args_status),
    "2": ("analyze", _args_analyze),
    "3": ("pipeline", _args_pipeline),
    "4": ("session", _args_session),
    "5": ("logs", _args_logs),
    "6": ("repro", _args_repro),
}

# Passthrough tool names — derived from the dispatch table so the two can't drift.
_TOOLS = tuple(tool for tool, _ in _MENU.values())

_USAGE = (
    f"usage: python -m debug [{'|'.join(_TOOLS)}] [args…]\n"
    "       python -m debug                  (대화형 메뉴)\n"
    "       python -m debug.<tool> --help    (도구별 옵션)"
)

_MENU_TEXT = """
============================================
 BUFS 디버그 도구  (q: 종료)
============================================
 1. 서버 상태 점검          (debug.status)
 2. 운영 통계 보기          (debug.analyze)
 3. 트레이스 1건 분석       (debug.pipeline)
 4. 세션 Q&A 조회           (debug.session)
 5. 로컬 로그 조회          (debug.logs)
 6. 모듈 단독 재실행        (debug.repro)
 q. 종료
"""


def _die_by_signal(sig: int) -> None:
    """Re-kill self with the child's fatal signal so the shell sees the same
    thing as a direct `python -m debug.<tool>` run ($?=128+N, 루프가 Ctrl-C에
    중단되는 동작 포함). POSIX 전용 — Windows에는 시그널 종료 상태가 없다."""
    try:
        signal.signal(sig, signal.SIG_DFL)
    except (OSError, ValueError):
        pass  # e.g. SIGKILL — default disposition already applies
    os.kill(os.getpid(), sig)


def _run(tool: str, args: list[str], *, verbose: bool = True) -> int:
    """Single dispatch point.

    verbose=True (menu mode): print the equivalent direct command before and an
    exit-code interpretation after. verbose=False (passthrough mode): add
    nothing to the child's output, so `python -m debug X` stays pipe-safe and
    truly equivalent to `python -m debug.X`.
    """
    cmd = [sys.executable, "-m", f"debug.{tool}", *args]
    if verbose:
        # Quoted for the shell the user will paste into — cmd.exe treats
        # POSIX single quotes as literal characters.
        shown = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
        print(f"\n$ python -m debug.{tool} {shown}".rstrip() + "\n")
    try:
        code = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        if verbose:
            print("\n(중단됨 — 메뉴로 돌아갑니다)")
        elif os.name == "posix":
            _die_by_signal(signal.SIGINT)  # passthrough keeps Ctrl-C shell semantics
        return 130
    if verbose:
        if code < 0:
            print(f"\n[debug.{tool}] 시그널 {-code}(으)로 종료")
        else:
            meaning = _EXIT_MEANINGS.get(tool, {}).get(code)
            label = f" — {meaning}" if meaning else ""
            print(f"\n[debug.{tool}] 종료코드 {code}{label}")
    elif code < 0 and os.name == "posix":
        _die_by_signal(-code)  # passthrough: child died by signal N → so do we
        return 128 - code  # fallback if the signal didn't terminate us (e.g. mocked in tests)
    return code


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Passthrough: python -m debug <tool> [args…] — adds nothing to the output
    if argv:
        tool = argv[0]
        if tool in ("-h", "--help"):
            print(_USAGE)
            return 0
        if tool in _TOOLS:
            return _run(tool, list(argv[1:]), verbose=False)
        print(f"error: unknown tool {tool!r}\n{_USAGE}", file=sys.stderr)
        return 2

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    while True:
        print(_MENU_TEXT)
        try:
            choice = _ask("선택: ").lower()
            if choice == "q":
                return 0
            entry = _MENU.get(choice)
            if entry is None:
                print("  ✗ 1~6 또는 q를 입력하세요")
                continue
            tool, builder = entry
            args = builder()
            if args is None:
                continue
            _run(tool, args)
        except KeyboardInterrupt:
            # Ctrl-C anywhere (menu prompt, builder prompt, or child run) goes
            # back to the menu — README §5 contract. Quit with q or Ctrl-D.
            print("\n(취소 — 메뉴로 돌아갑니다. 종료는 q 또는 Ctrl-D)")
            continue
        except EOFError:
            return 0


if __name__ == "__main__":
    sys.exit(main())
