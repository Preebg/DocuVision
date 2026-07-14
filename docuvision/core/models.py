"""Domain types for document jobs and pipeline outcomes.

``PipelineStatus`` is generated at import time from the shared JSON contract
``contracts/pipeline-status.json`` so Python and TypeScript stay bit-aligned.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "pipeline-status.json"
)


def _load_pipeline_status_enum() -> type[Enum]:
    with _CONTRACT_PATH.open(encoding="utf-8") as handle:
        contract = json.load(handle)
    stages: dict[str, str] = contract["STAGES"]
    return Enum("PipelineStatus", stages, type=str)


PipelineStatus = _load_pipeline_status_enum()

# Ordered happy-path sequence recorded by the orchestrator.
PIPELINE_HAPPY_PATH: tuple[PipelineStatus, ...] = (  # type: ignore[valid-type]
    PipelineStatus.QUEUED,
    PipelineStatus.SCANNING,
    PipelineStatus.LAYOUT,
    PipelineStatus.VISION,
    PipelineStatus.STAGING,
    PipelineStatus.PENDING_VERIFICATION,
)

# Backwards-compatible alias for adapters still importing JobStatus.
JobStatus = PipelineStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentJob(BaseModel):
    """Persistent job record for one inbound document."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    file_name: str
    status: PipelineStatus = PipelineStatus.QUEUED  # type: ignore[valid-type]
    retry_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    local_path: str | None = None
    layout_markdown: str | None = None
    extracted_payload: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    def transition_to(
        self,
        status: PipelineStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
        metadata: dict[str, Any] | None = None,
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
        if retry_count is not None:
            updates["retry_count"] = retry_count
        if metadata is not None:
            updates["metadata"] = metadata
        if error_message is not None:
            updates["error_message"] = error_message
        elif status != PipelineStatus.FAILED:
            updates["error_message"] = None
        return self.model_copy(update=updates)


class PipelineResult(BaseModel):
    """Outcome of a single document run through the state machine."""

    job_id: str
    file_name: str
    success: bool
    status: PipelineStatus
    extracted_payload: dict[str, Any] | None = None
    error_message: str | None = None
    correlation_id: str | None = None
