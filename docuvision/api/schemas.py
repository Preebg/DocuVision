"""Pydantic request/response models for the SPA-facing FastAPI surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentJobOut(BaseModel):
    """SPA-facing document row (mirrors DocumentPipelineRow)."""

    id: str
    created_at: str
    file_name: str | None = None
    extracted_payload: dict[str, Any] | None = None
    status: str | None = None
    error_message: str | None = None
    verified_payload: dict[str, Any] | None = None
    verified_by: str | None = None
    reject_reason: str | None = None
    storage_bucket: str | None = None
    processing_device: str | None = None
    processing_started_at: str | None = None


class QueueResponse(BaseModel):
    active_ingestion: list[DocumentJobOut] = Field(default_factory=list)
    pending_verification: list[DocumentJobOut] = Field(default_factory=list)


class DocumentPatchRequest(BaseModel):
    """HITL approve/reject body from the verification SPA."""

    action: Literal["approve", "reject"]
    verified_payload: dict[str, Any] | None = None
    verified_by: str | None = Field(
        default=None,
        description="Clerk / operator identity (LAN username, machine id, or email).",
    )
    actor: str | None = Field(
        default=None,
        description="Alias for verified_by; either body field or X-Operator-Identity header.",
    )
    reject_reason: str | None = None


class IngestResponse(BaseModel):
    id: str
    file_name: str
    status: str


class BatchDocumentOut(BaseModel):
    document_id: str
    sequence_no: int
    file_name: str | None = None
    status: str | None = None


class BatchIngestResponse(BaseModel):
    id: str
    name: str
    priority: int
    status: str
    source_id: str | None = None
    documents: list[BatchDocumentOut] = Field(default_factory=list)


class BatchPendingOut(BaseModel):
    id: str
    name: str
    priority: int
    status: str
    source_id: str | None = None
    document_count: int
    created_at: str
    updated_at: str


class BatchPendingResponse(BaseModel):
    batches: list[BatchPendingOut] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    repository: str
