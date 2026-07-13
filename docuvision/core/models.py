"""Domain types for document jobs and pipeline outcomes."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Explicit pipeline state-machine statuses.

    Happy-path transitions:
        QUEUED → DOWNLOADING → LAYOUT → VISION → STAGING → PENDING_VERIFICATION
    """

    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    LAYOUT = "LAYOUT"
    VISION = "VISION"
    STAGING = "STAGING"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    FAILED = "FAILED"


# Ordered happy-path sequence recorded by the orchestrator.
PIPELINE_HAPPY_PATH: tuple[JobStatus, ...] = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.LAYOUT,
    JobStatus.VISION,
    JobStatus.STAGING,
    JobStatus.PENDING_VERIFICATION,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentJob(BaseModel):
    """Persistent job record for one inbound document."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    file_name: str
    status: JobStatus = JobStatus.QUEUED
    local_path: str | None = None
    layout_markdown: str | None = None
    extracted_payload: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    def transition_to(
        self,
        status: JobStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> DocumentJob:
        """Return a copy advanced to ``status`` with optional field updates."""
        updates: dict[str, Any] = {
            "status": status,
            "updated_at": _utc_now(),
        }
        if local_path is not None:
            updates["local_path"] = local_path
        if layout_markdown is not None:
            updates["layout_markdown"] = layout_markdown
        if extracted_payload is not None:
            updates["extracted_payload"] = extracted_payload
        if error_message is not None:
            updates["error_message"] = error_message
        elif status != JobStatus.FAILED:
            updates["error_message"] = None
        return self.model_copy(update=updates)


class PipelineResult(BaseModel):
    """Outcome of a single document run through the state machine."""

    job_id: str
    file_name: str
    success: bool
    status: JobStatus
    extracted_payload: dict[str, Any] | None = None
    error_message: str | None = None
    correlation_id: str | None = None
