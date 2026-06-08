"""Q&A persistence as date-based JSONL (adapted from CamChat's app/logging/chat_logger.py).

Each answered turn is appended to  <repo>/logs/qa/qa_YYYY-MM-DD.jsonl  for analysis,
evaluation, debugging and reproduction. Skipped when CHAT_LOG_DISABLED env is set or a
request carries X-Test-Mode (per-request flag passed in from the router).
"""

import json
import logging
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

_QA_DIR = Path(config.LOG_DIR) / "qa"

# Per-request skip flag (set from the router when X-Test-Mode header is present).
_skip_var: ContextVar[bool] = ContextVar("agentic_rag_skip_qa_log", default=False)


def set_skip_log(flag: bool) -> None:
    _skip_var.set(bool(flag))


def should_skip_log() -> bool:
    return config.CHAT_LOG_DISABLED or _skip_var.get()


class QALogger:
    def __init__(self, log_dir: Path = _QA_DIR):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _today_path(self) -> Path:
        return self.log_dir / f"qa_{date.today().isoformat()}.jsonl"

    def log(
        self,
        *,
        question: str,
        answer: str,
        session_id: str = "",
        trace_id: str = "-",
        model: str = "",
        intent: str = "",
        duration_ms: int = 0,
        num_results: int = 0,
        sources: Optional[list[str]] = None,
        sub_questions: int = 0,
        tool_calls: int = 0,
        timing: Optional[dict] = None,
    ) -> None:
        if should_skip_log():
            return
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "trace_id": trace_id,
            "session_id": session_id,
            "model": model,
            "intent": intent,
            "question": question,
            "answer": answer,
            "duration_ms": duration_ms,
            "num_results": num_results,
            "sources": sources or [],
            "sub_questions": sub_questions,
            "tool_calls": tool_calls,
            "timing": timing or {},
        }
        try:
            with open(self._today_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 — logging must never break the request
            logger.error("Q&A log write failed: %s", exc)

    # ── read helpers (for analysis / eval) ──
    @staticmethod
    def _parse(path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        return out

    def read(self, d: Optional[date] = None) -> list[dict]:
        return self._parse(self.log_dir / f"qa_{(d or date.today()).isoformat()}.jsonl")

    def read_all(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self.log_dir.glob("qa_*.jsonl")):
            out.extend(self._parse(path))
        return out

    def list_dates(self) -> list[date]:
        dates: list[date] = []
        for path in sorted(self.log_dir.glob("qa_*.jsonl"), reverse=True):
            try:
                dates.append(date.fromisoformat(path.stem[3:]))  # "qa_YYYY-MM-DD" → date
            except ValueError:
                pass
        return dates


_qa_logger: Optional[QALogger] = None


def get_qa_logger() -> QALogger:
    global _qa_logger
    if _qa_logger is None:
        _qa_logger = QALogger()
    return _qa_logger
