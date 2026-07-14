"""Map ``DocumentJob`` domain objects to SPA DTOs."""

from __future__ import annotations

from docuvision.api.schemas import DocumentJobOut
from docuvision.core.models import DocumentJob


def job_to_out(job: DocumentJob) -> DocumentJobOut:
    meta = job.metadata or {}
    return DocumentJobOut(
        id=job.id,
        created_at=job.created_at.isoformat(),
        file_name=job.file_name,
        extracted_payload=job.extracted_payload,
        status=job.status.value if job.status is not None else None,
        error_message=job.error_message,
        verified_payload=meta.get("verified_payload"),
        verified_by=meta.get("verified_by"),
        reject_reason=meta.get("reject_reason"),
        storage_bucket=meta.get("storage_bucket"),
        processing_device=meta.get("processing_device"),
        processing_started_at=meta.get("processing_started_at"),
    )
