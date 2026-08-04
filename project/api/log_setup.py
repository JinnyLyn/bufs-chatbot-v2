"""Central logging configuration (ported from CamChat's backend/main.py).

- StreamHandler  → stdout (docker/console logs; lost on restart)
- TimedRotatingFileHandler → <repo>/logs/backend/app.log (host-persistent; daily rotation)

Both carry the request trace_id via TraceFilter, so every line is prefixed [trace_id]
and `grep <id>` reconstructs one request end-to-end.
"""

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import config
from api.trace_context import TraceFilter

_TRACE_FMT = "%(asctime)s [%(trace_id)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d - %(message)s"

# 쿼리스트링에 실려 오는 로그인 토큰. EventSource가 헤더를 못 붙여서 /api/chat/stream 만
# 쿼리로 받는데(api/chat.py), uvicorn 액세스 로그는 쿼리스트링을 통째로 찍는다 →
# 로그 파일에 재사용 가능한 자격증명이 평문으로 쌓인다. 기록 직전에 지운다.
_SECRET_QS_RE = re.compile(r"(access_token=)[^&\s\"']+")


class _RedactQuerySecrets(logging.Filter):
    """액세스 로그 한 줄에서 access_token 값을 가린다.

    uvicorn의 AccessFormatter는 record.args 에서 요청 라인을 조립하므로, 메시지가 아니라
    args 를 손봐야 한다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                _SECRET_QS_RE.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _SECRET_QS_RE.sub(r"\1<redacted>", record.msg)
        return True


class _SuppressOtelDetachWarning(logging.Filter):
    """OTel emits a benign 'Failed to detach context' warning with generators/contextvars.
    https://github.com/open-telemetry/opentelemetry-python/issues/2606"""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to detach context" not in record.getMessage()


def configure_logging() -> Path:
    """Install stream + rotating-file handlers with trace_id. Returns the log file path."""
    trace_filter = TraceFilter()
    formatter = logging.Formatter(_TRACE_FMT)

    log_dir = Path(config.LOG_DIR) / "backend"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=config.LOG_BACKUP_DAYS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(trace_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(trace_filter)

    logging.basicConfig(level=logging.INFO, force=True, handlers=[stream_handler, file_handler])

    # Silence the noisy OTel detach warning on the relevant logger.
    logging.getLogger("opentelemetry.context").addFilter(_SuppressOtelDetachWarning())

    # 액세스 로그에서 토큰 가리기. uvicorn.access 는 propagate=False 로 자체 핸들러를
    # 쓰므로 루트 핸들러 필터로는 안 잡히고, 로거에 직접 붙여야 한다. uvicorn 이 나중에
    # dictConfig 로 재설정해도 필터는 유지된다(핸들러만 교체한다).
    redactor = _RedactQuerySecrets()
    logging.getLogger("uvicorn.access").addFilter(redactor)
    for handler in (stream_handler, file_handler):
        handler.addFilter(redactor)
    # Quiet chatty third-party HTTP loggers (HuggingFace downloads, etc.).
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path
