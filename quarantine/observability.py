"""Structured JSON logging with per-document correlation IDs."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def derive_correlation_id(file_name: str | None = None) -> str:
    """UUID at ingestion start, or deterministic id from a file-name hash."""
    if file_name:
        digest = hashlib.sha256(file_name.encode("utf-8")).hexdigest()[:32]
        return str(uuid.UUID(digest))
    return str(uuid.uuid4())


def set_correlation_id(correlation_id: str) -> None:
    _correlation_id.set(correlation_id)


def get_correlation_id() -> str:
    current = _correlation_id.get()
    if current:
        return current
    generated = derive_correlation_id()
    _correlation_id.set(generated)
    return generated


def reset_correlation_id(correlation_id: str = "") -> None:
    _correlation_id.set(correlation_id)


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON including correlation_id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None)
            or get_correlation_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


class CorrelationIdFilter(logging.Filter):
    """Injects the active ContextVar correlation_id onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = get_correlation_id()
        return True


def configure_structured_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging for JSON stdout. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level)

    # Replace existing handlers so unit tests and re-entry stay deterministic.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())
    root.addHandler(handler)

    return logging.getLogger("docuvision")


def get_logger(name: str = "docuvision") -> logging.Logger:
    return logging.getLogger(name)
