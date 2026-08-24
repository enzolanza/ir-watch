"""Structured logging with credential redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime

from .config import LOCAL_TZ, get_settings

_SECRET_PATTERNS = [
    re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)"),          # user:pass@host
    re.compile(r"((?:password|passwd|pwd|token|api[_-]?key|secret)\s*[=:]\s*)(\S+)", re.I),
]


def redact(message: str) -> str:
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(lambda m: m.group(1) + "***" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""), message)
    return message


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.getMessage()))
            record.args = ()
        except Exception:  # noqa: BLE001 - never break logging
            pass
        return True


class LocalFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802 - stdlib signature
        stamp = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        return stamp.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")


class JSONFormatter(LocalFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    settings = get_settings()
    level = (level or settings.log_level).upper()
    fmt = (fmt or settings.log_format).lower()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(RedactingFilter())
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            LocalFormatter("%(asctime)s %(levelname)-5s %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("urllib3", "requests", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
