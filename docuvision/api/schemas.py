"""Pydantic request/response models for the SPA-facing FastAPI surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    verified_payload: dict[str, Any] | None = None
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


class IngestionSourceStatusOut(BaseModel):
    source_id: str
    display_name: str
    type: str
    path: str
    online: bool
    health: str
    queue_depth: int
    last_poll_at: str | None = None
    last_ingest_at: str | None = None
    last_error: str | None = None
    batches_formed: int = 0
    files_ingested: int = 0
    files_quarantined: int = 0
    listener: str = "none"


class IngestionStatusResponse(BaseModel):
    """Pillar 1 heartbeat — daemon liveness + per-source queue depth."""

    daemon: str = "ingestion-watcher"
    running: bool = False
    online: bool = False
    started_at: str | None = None
    updated_at: str | None = None
    stale: bool = True
    total_queue_depth: int = 0
    sources: list[IngestionSourceStatusOut] = Field(default_factory=list)


class QuarantineItemOut(BaseModel):
    id: str
    document_id: str
    file_name: str
    document_status: str
    lifecycle_status: str
    quarantine_reason: str
    error_stack: str | None = None
    failed_stage: str | None = None
    retry_count: int
    quarantined_at: str
    redrive_started_at: str | None = None
    batch_ids: list[str] = Field(default_factory=list)


class QuarantineItemsResponse(BaseModel):
    items: list[QuarantineItemOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class QuarantineRedriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_target(self) -> "QuarantineRedriveRequest":
        if (self.document_ids is None) == (self.batch_id is None):
            raise ValueError("Provide exactly one of document_ids or batch_id.")
        if self.document_ids is not None:
            cleaned = [item.strip() for item in self.document_ids]
            if any(not item or len(item) > 128 for item in cleaned):
                raise ValueError("Document IDs must be 1-128 characters.")
            if len(set(cleaned)) != len(cleaned):
                raise ValueError("document_ids must not contain duplicates.")
            self.document_ids = cleaned
        return self


class RedriveItemOut(BaseModel):
    document_id: str
    outcome: str
    status: str | None = None
    retry_count: int | None = None
    detail: str | None = None


class QuarantineRedriveResponse(BaseModel):
    results: list[RedriveItemOut] = Field(default_factory=list)
