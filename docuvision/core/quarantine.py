"""Crash-recoverable quarantine listing and redrive orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from docuvision.core.contracts import PipelineRepository, StorageBackend
from docuvision.core.models import (
    PipelineStatus,
    QuarantineItem,
    QuarantineReason,
    lifecycle_status,
)


@dataclass(frozen=True, slots=True)
class RedriveOutcome:
    document_id: str
    outcome: str
    status: str | None = None
    retry_count: int | None = None
    detail: str | None = None


class QuarantineService:
    def __init__(
        self,
        repository: PipelineRepository,
        storage: StorageBackend,
    ) -> None:
        self._repository = repository
        self._storage = storage

    def list_items(
        self,
        *,
        limit: int,
        offset: int,
        reason: QuarantineReason | None = None,
    ) -> tuple[list[QuarantineItem], int]:
        active, _ = self._repository.list_quarantine_items(
            limit=1000,
            offset=0,
            reason=reason,
        )
        active_ids = {item.document_id for item in active}
        failed_items: list[QuarantineItem] = []
        if reason in (None, QuarantineReason.PROCESSING_ERROR):
            for job in self._repository.list_by_statuses([PipelineStatus.FAILED]):
                if job.id in active_ids:
                    continue
                batches = self._repository.list_batches_for_document(job.id)
                failed_items.append(
                    QuarantineItem(
                        id=f"failed:{job.id}",
                        document_id=job.id,
                        file_name=job.file_name,
                        document_status=job.status,
                        lifecycle_status=lifecycle_status(job.status),
                        reason=QuarantineReason.PROCESSING_ERROR,
                        error_stack=job.metadata.get("error_traceback")
                        or job.metadata.get("last_transient_traceback"),
                        failed_stage=job.metadata.get("failed_stage"),
                        retry_count=job.retry_count,
                        quarantined_at=job.updated_at,
                        batch_ids=[batch.id for batch in batches],
                    )
                )
        combined = sorted(
            [*active, *failed_items],
            key=lambda item: item.quarantined_at,
            reverse=True,
        )
        return combined[offset : offset + limit], len(combined)

    def redrive(
        self,
        document_ids: list[str],
        *,
        actor: str,
    ) -> list[RedriveOutcome]:
        return [self._redrive_one(document_id, actor=actor) for document_id in document_ids]

    def _redrive_one(self, document_id: str, *, actor: str) -> RedriveOutcome:
        job = self._repository.get_job(document_id)
        if job is None:
            return RedriveOutcome(document_id, "not_found", detail="Document not found.")
        if job.status not in {PipelineStatus.QUARANTINED, PipelineStatus.FAILED}:
            return RedriveOutcome(
                document_id,
                "invalid_state",
                status=job.status.value,
                detail="Expected QUARANTINED or FAILED.",
            )

        item = self._repository.get_active_quarantine(document_id)
        if item is None:
            item = self._repository.begin_quarantine(
                document_id,
                reason=QuarantineReason.PROCESSING_ERROR,
                error_stack=job.error_message or "Legacy FAILED document.",
                failed_stage=str(job.metadata.get("failed_stage") or "UNKNOWN"),
            )
            try:
                locator = self._storage.quarantine_payload(
                    job.id,
                    job.file_name,
                    job.local_path,
                )
                self._repository.set_quarantine_locator(item.id, locator)
                item = self._repository.get_active_quarantine(document_id) or item
            except FileNotFoundError:
                # An interrupted prior redrive may already have restored ingress.
                pass

        token = str(uuid4())
        reserved = self._repository.reserve_redrive(
            document_id,
            actor=actor,
            token=token,
        )
        if reserved is None:
            return RedriveOutcome(
                document_id,
                "conflict",
                status=PipelineStatus.QUARANTINED.value,
                detail="Another redrive owns this document.",
            )
        try:
            self._storage.restore_for_redrive(
                reserved.file_name,
                reserved.payload_locator,
            )
            queued = self._repository.finalize_redrive(
                document_id,
                actor=actor,
                token=token,
            )
        except (FileNotFoundError, FileExistsError, ValueError, KeyError) as exc:
            return RedriveOutcome(
                document_id,
                "error",
                status=PipelineStatus.QUARANTINED.value,
                detail=str(exc),
            )
        return RedriveOutcome(
            document_id,
            "queued",
            status=queued.status.value,
            retry_count=queued.retry_count,
        )


__all__ = ["QuarantineService", "RedriveOutcome"]
