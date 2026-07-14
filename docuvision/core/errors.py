"""Structured pipeline exception taxonomy for air-gapped orchestration."""

from __future__ import annotations


class PipelineError(Exception):
    """Base error for all pipeline failures."""

    def __init__(
        self,
        message: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id


class TransientError(PipelineError):
    """Retryable network / API timeouts and short-lived dependency flaps."""


class ValidationError(PipelineError):
    """Corrupt PDFs, schema validation failures, or malformed payloads."""


class InfrastructureError(PipelineError):
    """Database disconnects, VRAM OOM, or other environment failures."""


class FatalProcessingError(PipelineError):
    """Unrecoverable processing failures that must not be retried blindly."""
