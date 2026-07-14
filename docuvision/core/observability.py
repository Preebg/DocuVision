"""Structured JSON logging for on-prem pipeline observability."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Serialize each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
        }
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id:
            payload["correlation_id"] = correlation_id
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logger(
    name: str = "docuvision",
    *,
    level: int = logging.INFO,
) -> logging.Logger:
    """Return a logger that emits single-line JSON via :class:`JsonFormatter`."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.handlers.clear()
        logger.addHandler(handler)

    return logger


def bind_correlation(
    logger: logging.Logger,
    correlation_id: str,
) -> logging.LoggerAdapter[logging.Logger]:
    """Attach a stable ``correlation_id`` to every log emitted for a job."""
    return logging.LoggerAdapter(logger, {"correlation_id": correlation_id})
