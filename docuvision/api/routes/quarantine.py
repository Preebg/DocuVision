"""Administrative quarantine diagnostics and bulk redrive routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from docuvision.api.deps import get_repository, get_storage
from docuvision.api.operator_auth import (
    OperatorPrincipal,
    require_supervisor_or_admin,
)
from docuvision.api.schemas import (
    QuarantineItemOut,
    QuarantineItemsResponse,
    QuarantineRedriveRequest,
    QuarantineRedriveResponse,
    RedriveItemOut,
)
from docuvision.core.contracts import PipelineRepository
from docuvision.core.models import QuarantineReason
from docuvision.core.quarantine import QuarantineService

router = APIRouter(prefix="/api/v1/quarantine", tags=["quarantine"])


@router.get("/items", response_model=QuarantineItemsResponse)
def list_quarantine_items(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    reason: QuarantineReason | None = Query(default=None),
    repository: PipelineRepository = Depends(get_repository),
    storage=Depends(get_storage),
    _operator: OperatorPrincipal = Depends(require_supervisor_or_admin),
) -> QuarantineItemsResponse:
    items, total = QuarantineService(repository, storage).list_items(
        limit=limit,
        offset=offset,
        reason=reason,
    )
    return QuarantineItemsResponse(
        items=[
            QuarantineItemOut(
                id=item.id,
                document_id=item.document_id,
                file_name=item.file_name,
                document_status=item.document_status.value,
                lifecycle_status=item.lifecycle_status.value,
                quarantine_reason=item.reason.value,
                error_stack=item.error_stack,
                failed_stage=item.failed_stage,
                retry_count=item.retry_count,
                quarantined_at=item.quarantined_at.isoformat(),
                redrive_started_at=item.redrive_started_at.isoformat()
                if item.redrive_started_at
                else None,
                batch_ids=item.batch_ids,
            )
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/redrive", response_model=QuarantineRedriveResponse)
def redrive_quarantine_items(
    body: QuarantineRedriveRequest,
    response: Response,
    repository: PipelineRepository = Depends(get_repository),
    storage=Depends(get_storage),
    operator: OperatorPrincipal = Depends(require_supervisor_or_admin),
) -> QuarantineRedriveResponse:
    if body.batch_id is not None:
        batch = repository.get_batch(body.batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found.")
        document_ids = [link.document_id for link in batch.documents]
        if len(document_ids) > 100:
            raise HTTPException(
                status_code=413,
                detail="Batch exceeds the 100-document redrive limit.",
            )
    else:
        document_ids = body.document_ids or []

    outcomes = QuarantineService(repository, storage).redrive(
        document_ids,
        actor=operator.id,
    )
    if outcomes and any(item.outcome != "queued" for item in outcomes):
        response.status_code = status.HTTP_207_MULTI_STATUS
    return QuarantineRedriveResponse(
        results=[
            RedriveItemOut(
                document_id=item.document_id,
                outcome=item.outcome,
                status=item.status,
                retry_count=item.retry_count,
                detail=item.detail,
            )
            for item in outcomes
        ]
    )
