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


class ReleaseExportError(InfrastructureError):
    """Filesystem / network-mount failure while exporting a verified batch.

    Catchable by the release orchestrator so the worker/API loop stays up and
    the batch transitions to ``release_failed`` for automated retry.
    """


class ReleaseSerializationError(ReleaseExportError):
    """A specific document could not be serialized for release."""

    def __init__(
        self,
        serializer: str,
        document_id: str,
        detail: str,
    ) -> None:
        self.serializer = serializer
        self.document_id = document_id
        self.detail = detail
        super().__init__(
            f"{serializer.upper()} serialization failed for document "
            f"{document_id}: {detail}"
        )
