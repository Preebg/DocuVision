"""Release Engine (Pillar 5) — batch status transitions + export orchestration.

State machine (batch)::

    ready_for_hitl → releasing → released
                              ↘ release_failed  (retry → releasing)

Clerk validation is preserved across ``release_failed`` so automated retry
never forces re-entry of verified fields.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.release.base import ReleaseExporter
from docuvision.core.errors import ReleaseExportError
from docuvision.core.models import Batch, BatchStatus, DocumentJob, PipelineStatus

logger = logging.getLogger(__name__)


class _ReleaseRepository(Protocol):
    """Narrow repository surface required by the release engine."""

    def get_job(self, job_id: str) -> DocumentJob | None: ...

    def get_batch(self, batch_id: str) -> Batch | None: ...

    def list_batches_for_document(self, document_id: str) -> list[Batch]: ...

    def maybe_mark_batches_ready_for_hitl(self, document_id: str) -> list[Batch]: ...

    def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Batch: ...

    def emit_audit_event(
        self,
        *,
        job_id: str,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class ReleaseEngine:
    """Coordinates ready-for-HITL detection and post-verification export."""

    def __init__(
        self,
        repository: _ReleaseRepository,
        storage: LocalStorageAdapter,
        exporter: ReleaseExporter,
        *,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._exporter = exporter
        self._log = logger_ or logger

    def mark_ready_for_hitl_if_complete(self, document_id: str) -> list[Batch]:
        """Flip parent batch(es) ``processing`` → ``ready_for_hitl`` when due."""
        advanced = self._repository.maybe_mark_batches_ready_for_hitl(document_id)
        for updated in advanced:
            self._audit_batch(
                batch=updated,
                actor="release-engine",
                action="BATCH_READY_FOR_HITL",
                from_status=BatchStatus.PROCESSING.value,
                to_status=BatchStatus.READY_FOR_HITL.value,
                details={"trigger_document_id": document_id},
            )
            self._log.info(
                "release.batch_ready_for_hitl batch_id=%s name=%s",
                updated.id,
                updated.name,
            )
        return advanced

    async def release_after_document_verified(
        self,
        document_id: str,
        *,
        actor: str,
    ) -> list[Batch]:
        """If every member is ``VERIFIED``, run export for eligible batches."""
        # Cover the race where the last verify lands before ready_for_hitl.
        self.mark_ready_for_hitl_if_complete(document_id)

        released: list[Batch] = []
        for batch in self._repository.list_batches_for_document(document_id):
            if batch.status not in {
                BatchStatus.READY_FOR_HITL,
                BatchStatus.RELEASE_FAILED,
            }:
                continue
            if not self._all_documents_verified(batch):
                continue
            result = await self.release_batch(batch.id, actor=actor)
            released.append(result)
        return released

    async def release_batch(self, batch_id: str, *, actor: str) -> Batch:
        """Execute ``ready_for_hitl|release_failed`` → ``releasing`` → terminal.

        Export failures transition to ``release_failed`` and never raise out of
        this method — the worker/API loop stays alive.
        """
        batch = self._repository.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"Batch not found: {batch_id}")

        if batch.status not in {
            BatchStatus.READY_FOR_HITL,
            BatchStatus.RELEASE_FAILED,
            BatchStatus.RELEASING,
        }:
            raise ValueError(
                f"Batch {batch_id} cannot release from status {batch.status.value}"
            )

        if not self._all_documents_verified(batch):
            raise ValueError(
                f"Batch {batch_id} still has unverified documents; refusing release."
            )

        from_status = batch.status.value
        batch = self._repository.update_batch_status(
            batch_id,
            BatchStatus.RELEASING,
            actor=actor,
            details={"previous_status": from_status},
        )
        self._audit_batch(
            batch=batch,
            actor=actor,
            action="BATCH_RELEASING",
            from_status=from_status,
            to_status=BatchStatus.RELEASING.value,
        )

        try:
            payload = self._build_export_payload(batch)
            ok = await self._exporter.export_batch(batch_id, payload)
            if not ok:
                raise ReleaseExportError(
                    f"Exporter returned False for batch {batch_id}"
                )
        except (ReleaseExportError, OSError, TimeoutError, FileNotFoundError) as exc:
            self._log.error(
                "release.export_failed batch_id=%s error=%s",
                batch_id,
                exc,
            )
            failed = self._repository.update_batch_status(
                batch_id,
                BatchStatus.RELEASE_FAILED,
                actor=actor,
                details={"error": str(exc)},
            )
            self._audit_batch(
                batch=failed,
                actor=actor,
                action="BATCH_RELEASE_FAILED",
                from_status=BatchStatus.RELEASING.value,
                to_status=BatchStatus.RELEASE_FAILED.value,
                details={"error": str(exc)},
            )
            return failed
        except Exception as exc:
            # Never crash the worker loop on unexpected exporter faults.
            self._log.exception(
                "release.export_unexpected batch_id=%s error=%s",
                batch_id,
                exc,
            )
            failed = self._repository.update_batch_status(
                batch_id,
                BatchStatus.RELEASE_FAILED,
                actor=actor,
                details={"error": str(exc)},
            )
            self._audit_batch(
                batch=failed,
                actor=actor,
                action="BATCH_RELEASE_FAILED",
                from_status=BatchStatus.RELEASING.value,
                to_status=BatchStatus.RELEASE_FAILED.value,
                details={"error": str(exc), "unexpected": True},
            )
            return failed

        released = self._repository.update_batch_status(
            batch_id,
            BatchStatus.RELEASED,
            actor=actor,
            details={"released_at": datetime.now(timezone.utc).isoformat()},
        )
        self._audit_batch(
            batch=released,
            actor=actor,
            action="BATCH_RELEASED",
            from_status=BatchStatus.RELEASING.value,
            to_status=BatchStatus.RELEASED.value,
            details={"package_batch_name": batch.name},
        )
        self._log.info(
            "release.batch_released batch_id=%s name=%s",
            released.id,
            released.name,
        )
        return released

    def _all_documents_verified(self, batch: Batch) -> bool:
        if not batch.documents:
            return False
        for link in batch.documents:
            job = self._repository.get_job(link.document_id)
            if job is None or job.status != PipelineStatus.VERIFIED:
                return False
        return True

    def _build_export_payload(self, batch: Batch) -> dict[str, Any]:
        documents: list[dict[str, Any]] = []
        for link in sorted(batch.documents, key=lambda item: item.sequence_no):
            job = self._repository.get_job(link.document_id)
            if job is None:
                raise ReleaseExportError(
                    f"Missing job {link.document_id} while building release "
                    f"payload for batch {batch.id}"
                )
            file_name = link.file_name or job.file_name
            try:
                source = self._storage.resolve_document_path(
                    file_name,
                    prefer_verification=True,
                )
            except FileNotFoundError as exc:
                raise ReleaseExportError(str(exc)) from exc

            page_rows = getattr(self._repository, "list_document_pages", None)
            page_paths: list[str] = []
            if callable(page_rows):
                pages = page_rows(link.document_id)
                page_paths = [page.file_path for page in pages if page.file_path]

            verified_payload = job.metadata.get("verified_payload")
            if not isinstance(verified_payload, dict):
                verified_payload = job.extracted_payload
            fields = _extract_fields(verified_payload)

            documents.append(
                {
                    "document_id": job.id,
                    "sequence_no": link.sequence_no,
                    "file_name": file_name,
                    "source_path": str(source),
                    "page_paths": page_paths,
                    "extracted_payload": job.extracted_payload,
                    "verified_payload": verified_payload,
                    "verified_by": job.metadata.get("verified_by"),
                    "status": job.status.value,
                    "timestamp": job.updated_at.isoformat(),
                    "fields": fields,
                }
            )

        return {
            "batch_id": batch.id,
            "batch_name": batch.name,
            "batch": {
                "id": batch.id,
                "name": batch.name,
                "priority": batch.priority,
                "source_id": batch.source_id,
                "status": batch.status.value,
            },
            "documents": documents,
        }

    def _audit_batch(
        self,
        *,
        batch: Batch,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record batch transitions against the first linked document (audit FK)."""
        if not batch.documents:
            return
        anchor = batch.documents[0].document_id
        payload = {"batch_id": batch.id, "batch_name": batch.name, **(details or {})}
        try:
            self._repository.emit_audit_event(
                job_id=anchor,
                actor=actor,
                action=action,
                from_status=from_status,
                to_status=to_status,
                details=payload,
            )
        except Exception:
            self._log.exception(
                "release.audit_failed batch_id=%s action=%s",
                batch.id,
                action,
            )


def _extract_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
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
