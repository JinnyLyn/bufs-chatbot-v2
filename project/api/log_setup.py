"""Central logging configuration (ported from CamChat's backend/main.py).

- StreamHandler  → stdout (docker/console logs; lost on restart)
- TimedRotatingFileHandler → <repo>/logs/backend/app.log (host-persistent; daily rotation)

Both carry the request trace_id via TraceFilter, so every line is prefixed [trace_id]
and `grep <id>` reconstructs one request end-to-end.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import config
from api.trace_context import TraceFilter

_TRACE_FMT = "%(asctime)s [%(trace_id)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d - %(message)s"


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
    # Quiet chatty third-party HTTP loggers (HuggingFace downloads, etc.).
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path
