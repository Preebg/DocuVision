"""Document ingest, file streaming, and HITL approve/reject."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from docuvision.adapters.local_storage import PROCESSABLE_SUFFIXES, LocalStorageAdapter
from docuvision.api.deps import get_repository, get_storage
from docuvision.api.schemas import DocumentJobOut, DocumentPatchRequest, IngestResponse
from docuvision.api.serializers import job_to_out
from docuvision.core.contracts import PipelineRepository
from docuvision.core.models import PipelineStatus

router = APIRouter(tags=["documents"])

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")
_OPERATOR_HEADER = "X-Operator-Identity"


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


def _resolve_operator_identity(
    body: DocumentPatchRequest,
    x_operator_identity: str | None,
) -> str:
    """Prefer header, then body.actor, then body.verified_by."""
    for candidate in (
        x_operator_identity,
        body.actor,
        body.verified_by,
    ):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    raise HTTPException(
        status_code=400,
        detail=(
            "Operator identity is required. Pass X-Operator-Identity header "
            "or verified_by / actor in the request body."
        ),
    )


def _payload_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload or not isinstance(payload, dict):
        return {}
    nested = payload.get("extracted_payload")
    if isinstance(nested, dict):
        fields = nested.get("fields")
        if isinstance(fields, dict):
            return dict(fields)
    fields = payload.get("fields")
    if isinstance(fields, dict):
        return dict(fields)
    return {}


def _build_clerk_audit_details(
    *,
    original_payload: dict[str, Any] | None,
    verified_payload: dict[str, Any] | None,
    reject_reason: str | None = None,
) -> dict[str, Any]:
    """Structured diff: AI extraction vs clerk-committed fields."""
    original_fields = _payload_fields(original_payload)
    final_fields = _payload_fields(verified_payload) if verified_payload else {}
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(original_fields) | set(final_fields)):
        before = original_fields.get(key)
        after = final_fields.get(key)
        if before != after:
            changed[key] = {"from": before, "to": after}

    details: dict[str, Any] = {
        "original_extracted_payload": original_payload,
        "verified_payload": verified_payload,
        "original_fields": original_fields,
        "final_fields": final_fields,
        "changed_fields": changed,
    }
    if reject_reason is not None:
        details["reject_reason"] = reject_reason
    return details


@router.post("/api/documents/ingest", response_model=IngestResponse)
async def ingest_document(
    file: UploadFile = File(...),
    storage: LocalStorageAdapter = Depends(get_storage),
    repository: PipelineRepository = Depends(get_repository),
) -> IngestResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name.")

    safe_name = _sanitize_file_name(file.filename)
    # Prefix with timestamp in the client historically; keep basename unique via storage.
    data = await file.read()
    stored_name = storage.write_incoming(safe_name, data)
    job = repository.create_job(stored_name)
    return IngestResponse(
        id=job.id,
        file_name=job.file_name,
        status=job.status.value,
    )


@router.get("/api/documents/{job_id}/file")
def stream_document_file(
    job_id: str,
    storage: LocalStorageAdapter = Depends(get_storage),
    repository: PipelineRepository = Depends(get_repository),
) -> FileResponse:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    prefer_verification = job.status.value == PipelineStatus.PENDING_VERIFICATION.value
    # Also prefer verification once staged or beyond.
    if job.status.value in {
        PipelineStatus.STAGING.value,
        PipelineStatus.PENDING_VERIFICATION.value,
        PipelineStatus.VERIFIED.value,
    }:
        prefer_verification = True

    try:
        path = storage.resolve_document_path(
            job.file_name,
            prefer_verification=prefer_verification,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=path.name,
    )


@router.patch("/api/documents/{job_id}", response_model=DocumentJobOut)
def patch_document(
    job_id: str,
    body: DocumentPatchRequest,
    repository: PipelineRepository = Depends(get_repository),
    x_operator_identity: str | None = Header(default=None, alias=_OPERATOR_HEADER),
) -> DocumentJobOut:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    if job.status != PipelineStatus.PENDING_VERIFICATION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job is {job.status.value}, expected PENDING_VERIFICATION. "
                "State machine bypass rejected."
            ),
        )

    actor = _resolve_operator_identity(body, x_operator_identity)
    original_payload = job.extracted_payload
    meta = dict(job.metadata)

    if body.action == "approve":
        verified_payload = body.verified_payload or original_payload
        meta["verified_payload"] = verified_payload
        meta["verified_by"] = actor
        meta.pop("reject_reason", None)
        updated = repository.update_status(
            job_id,
            PipelineStatus.VERIFIED,
            metadata=meta,
        )
        repository.emit_audit_event(
            job_id=job_id,
            actor=actor,
            action="CLERK_VERIFY_APPROVE",
            from_status=PipelineStatus.PENDING_VERIFICATION.value,
            to_status=PipelineStatus.VERIFIED.value,
            details=_build_clerk_audit_details(
                original_payload=original_payload,
                verified_payload=verified_payload
                if isinstance(verified_payload, dict)
                else None,
            ),
        )
    else:
        reject_reason = body.reject_reason or "UNREADABLE_DOCUMENT"
        # Preserve any clerk field overrides even on reject for non-repudiation.
        verified_payload = body.verified_payload
        meta["reject_reason"] = reject_reason
        meta["verified_by"] = actor
        if verified_payload is not None:
            meta["verified_payload"] = verified_payload
        updated = repository.update_status(
            job_id,
            PipelineStatus.FAILED,
            error_message=reject_reason,
            metadata=meta,
        )
        repository.emit_audit_event(
            job_id=job_id,
            actor=actor,
            action="CLERK_VERIFY_REJECT",
            from_status=PipelineStatus.PENDING_VERIFICATION.value,
            to_status=PipelineStatus.FAILED.value,
            details=_build_clerk_audit_details(
                original_payload=original_payload,
                verified_payload=verified_payload
                if isinstance(verified_payload, dict)
                else None,
                reject_reason=reject_reason,
            ),
        )

    return job_to_out(updated)
