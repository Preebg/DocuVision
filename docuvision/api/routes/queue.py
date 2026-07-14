"""Queue listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from docuvision.api.deps import get_repository
from docuvision.api.schemas import QueueResponse
from docuvision.api.serializers import job_to_out
from docuvision.core.contracts import PipelineRepository
from docuvision.core.models import PipelineStatus

router = APIRouter(tags=["queue"])

ACTIVE_INGESTION_STATUSES = (
    PipelineStatus.SCANNING,
    PipelineStatus.LAYOUT,
    PipelineStatus.VISION,
)


@router.get("/api/queue", response_model=QueueResponse)
def get_queue(
    repository: PipelineRepository = Depends(get_repository),
) -> QueueResponse:
    active = repository.list_by_statuses(ACTIVE_INGESTION_STATUSES)  # type: ignore[attr-defined]
    pending = repository.list_by_statuses(  # type: ignore[attr-defined]
        (PipelineStatus.PENDING_VERIFICATION,)
    )
    return QueueResponse(
        active_ingestion=[job_to_out(job) for job in active],
        pending_verification=[job_to_out(job) for job in pending],
    )
