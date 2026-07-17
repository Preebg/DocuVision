"""Batch ingest and supervisor pending-queue endpoints."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from docuvision.adapters.local_storage import PROCESSABLE_SUFFIXES, LocalStorageAdapter
from docuvision.api.deps import (
    get_batch_split_engine,
    get_repository,
    get_storage,
)
from docuvision.api.schemas import (
    BatchDocumentOut,
    BatchIngestResponse,
    BatchPendingOut,
    BatchPendingResponse,
    BatchSplitRequest,
    BatchSplitResponse,
)
from docuvision.core.batch_splitting import (
    BatchSplitEngine,
    PdfSplitError,
)
from docuvision.core.contracts import PipelineRepository

router = APIRouter(tags=["batches"])

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize_file_name(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    suffix = Path(cleaned).suffix.lower()
    if suffix not in PROCESSABLE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {suffix or '(none)'}",
        )
    return cleaned


@router.post("/api/batches/ingest", response_model=BatchIngestResponse)
async def ingest_batch(
    files: list[UploadFile] = File(..., description="Ordered documents in the batch"),
    name: str = Form(..., description='Batch label, e.g. "NYSDOH_Ingest_2026_07_14"'),
    priority: int = Form(2, description="1=Low, 2=Med, 3=High"),
    source_id: str | None = Form(None, description="Optional origin folder / scanner id"),
    storage: LocalStorageAdapter = Depends(get_storage),
    repository: PipelineRepository = Depends(get_repository),
) -> BatchIngestResponse:
    """Accept a multi-document batch wrapper (multipart) into the ingress queue."""
    batch_name = (name or "").strip()
    if not batch_name:
        raise HTTPException(status_code=400, detail="Batch name is required.")
    if priority not in (1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="priority must be 1 (Low), 2 (Med), or 3 (High).",
        )
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    payload: list[tuple[str, bytes]] = []
    for upload in files:
        if not upload.filename:
            raise HTTPException(status_code=400, detail="Missing file name.")
        safe_name = _sanitize_file_name(upload.filename)
        data = await upload.read()
        if not data:
            raise HTTPException(
                status_code=400,
                detail=f"Empty upload rejected: {safe_name}",
            )
        payload.append((safe_name, data))

    stored_names = storage.write_incoming_batch(payload)
    links: list[tuple[str, int]] = []
    document_outs: list[BatchDocumentOut] = []
    for sequence_no, stored_name in enumerate(stored_names, start=1):
        job = repository.create_job(stored_name)
        links.append((job.id, sequence_no))
        document_outs.append(
            BatchDocumentOut(
                document_id=job.id,
                sequence_no=sequence_no,
                file_name=job.file_name,
                status=job.status.value,
            )
        )

    batch = repository.create_batch(
        name=batch_name,
        priority=priority,
        source_id=(source_id.strip() if source_id and source_id.strip() else None),
        documents=links,
    )
    return BatchIngestResponse(
        id=batch.id,
        name=batch.name,
        priority=batch.priority,
        status=batch.status.value,
        source_id=batch.source_id,
        documents=document_outs,
    )


@router.post(
    "/api/batches/{batch_id}/split",
    response_model=BatchSplitResponse,
)
def process_batch_splits(
    batch_id: str,
    request: BatchSplitRequest,
    storage: LocalStorageAdapter = Depends(get_storage),
    repository: PipelineRepository = Depends(get_repository),
    engine: BatchSplitEngine = Depends(get_batch_split_engine),
) -> BatchSplitResponse:
    """Detect physical separators and replace one source with queued children."""
    job = repository.get_job(request.source_document_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Source document not found.")
    try:
        source_path = storage.resolve_document_path(
            job.file_name, prefer_verification=False
        )
        result = engine.process_batch_splits(
            batch_id,
            str(source_path),
            request.strategy_config,
            source_document_id=job.id,
        )
        return BatchSplitResponse.model_validate(result.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except PdfSplitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/batches/pending", response_model=BatchPendingResponse)
def list_pending_batches(
    repository: PipelineRepository = Depends(get_repository),
) -> BatchPendingResponse:
    """Batch-level summaries for supervisor / queue views."""
    summaries = repository.list_pending_batches()
    return BatchPendingResponse(
        batches=[
            BatchPendingOut(
                id=item.id,
                name=item.name,
                priority=item.priority,
                status=item.status.value,
                source_id=item.source_id,
                document_count=item.document_count,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
            for item in summaries
        ]
    )
