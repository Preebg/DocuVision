"""Structural contracts for external backends (dependency inversion).

``DocumentPipeline`` depends only on these Protocols. Concrete adapters
(local filesystem, SQLite, Ollama HTTP client) live under ``docuvision.adapters``
and are wired at composition-root time.
"""

from __future__ import annotations

from typing import Any, BinaryIO, Protocol, Sequence, runtime_checkable

from docuvision.core.models import (
    Batch,
    BatchStatus,
    BatchSummary,
    DocumentJob,
    DocumentPage,
    PipelineStatus,
    QuarantineItem,
    QuarantineReason,
)


@runtime_checkable
class StorageBackend(Protocol):
    """On-prem document ingress / egress without leaking path layout to core.

    Implementations map logical operations onto configured directories
    (incoming / processed / failed / verification staging). The pipeline never
    reads those paths — it only receives opaque local file handles.
    """

    def list_incoming(self) -> list[str]:
        """Return processable file names waiting in the ingress area."""
        ...

    def fetch_to_workspace(self, file_name: str) -> str:
        """Copy or move ``file_name`` into a workspace path; return that path."""
        ...

    def stage_for_verification(self, file_name: str, local_path: str) -> str:
        """Place the source document where HITL verification can read it.

        Returns a locator string meaningful to the storage adapter
        (path, URI, or bucket key) — opaque to the orchestrator.
        """
        ...

    def mark_processed(self, file_name: str, local_path: str) -> None:
        """Archive a successfully processed document out of the ingress queue."""
        ...

    def mark_failed(self, file_name: str, local_path: str | None) -> None:
        """Move a failed document into the dead-letter / failed area."""
        ...

    def quarantine_payload(
        self,
        document_id: str,
        file_name: str,
        local_path: str | None,
    ) -> str:
        """Isolate a payload and return an opaque, non-client-facing locator."""
        ...

    def restore_for_redrive(
        self,
        file_name: str,
        payload_locator: str | None,
    ) -> str:
        """Idempotently restore a quarantined/failed payload to ingress."""
        ...

    def write_incoming_batch(
        self,
        documents: Sequence[tuple[str, BinaryIO | bytes]],
    ) -> list[str]:
        """Write an ordered batch of documents into the ingress directory.

        Each item is ``(original_file_name, bytes_or_stream)``. Returns the
        stored basenames in the same sequence (may be uniquified on collision).
        """
        ...

    def remove_incoming(self, file_names: Sequence[str]) -> None:
        """Delete only the named ingress files during a failed atomic handoff."""
        ...


@runtime_checkable
class PipelineRepository(Protocol):
    """Durable job / batch tracking and status persistence."""

    def create_job(self, file_name: str) -> DocumentJob:
        """Insert a new job in ``QUEUED`` and return it."""
        ...

    def create_batch(
        self,
        *,
        name: str,
        priority: int = 2,
        source_id: str | None = None,
        documents: Sequence[tuple[str, int]],
    ) -> Batch:
        """Create a batch and link ``(document_id, sequence_no)`` members.

        Documents must already exist (via ``create_job``). Returns the batch
        with membership ordered by ``sequence_no``.
        """
        ...

    def list_pending_batches(self) -> list[BatchSummary]:
        """Return non-terminal batches for the supervisor queue view.

        Ordered by priority (HIGH first), then ``created_at`` ascending.
        """
        ...

    def get_batch(self, batch_id: str) -> Batch | None:
        """Load a batch with ordered document links, or ``None`` if missing."""
        ...

    def list_batches_for_document(self, document_id: str) -> list[Batch]:
        """Return batches that include ``document_id`` (ordered membership)."""
        ...

    def get_batch_split_children(
        self,
        batch_id: str,
        parent_document_id: str,
    ) -> list[tuple[str, str, int, int, int, int]]:
        """Return child id/name, part index, page range, and batch sequence."""
        ...

    def commit_batch_split(
        self,
        batch_id: str,
        parent_document_id: str,
        children: Sequence[tuple[str, int, int]],
        *,
        strategy: str,
    ) -> list[DocumentJob]:
        """Atomically replace a source batch member with queued child jobs."""
        ...

    def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Batch:
        """Persist a batch status transition and return the updated batch."""
        ...

    def list_document_pages(self, document_id: str) -> list[DocumentPage]:
        """Return ordered page fragments for a document (may be empty)."""
        ...

    def maybe_mark_batches_ready_for_hitl(self, document_id: str) -> list[Batch]:
        """Advance parent ``processing`` batches to ``ready_for_hitl`` when due.

        A batch qualifies when every linked document is
        ``PENDING_VERIFICATION`` or ``VERIFIED``.
        """
        ...

    def add_document_pages(
        self,
        document_id: str,
        pages: Sequence[tuple[int, str]],
    ) -> list[DocumentPage]:
        """Record page fragments ``(page_no, file_path)`` for a document."""
        ...

    def save_job(self, job: DocumentJob) -> DocumentJob:
        """Persist the full job snapshot (status + payloads)."""
        ...

    def update_status(
        self,
        job_id: str,
        status: PipelineStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentJob:
        """Transition and persist a job's state."""
        ...

    def mark_failed(self, job_id: str, error_message: str) -> DocumentJob:
        """Record terminal ``FAILED`` status with diagnostic text."""
        ...

    def get_job(self, job_id: str) -> DocumentJob | None:
        """Load a job by id, or ``None`` if missing."""
        ...

    def list_by_statuses(
        self,
        statuses: Sequence[PipelineStatus],
    ) -> list[DocumentJob]:
        """List jobs whose detailed state is in ``statuses``."""
        ...

    def claim_next_queued(self) -> DocumentJob | None:
        """Atomically claim the next ``QUEUED`` job for extraction.

        Prefer documents belonging to HIGH-priority batches first, then by
        ``created_at``. Standalone (non-batch) documents use default medium
        priority. Implementations must guarantee concurrent workers never
        receive the same document: PostgreSQL uses
        ``SELECT … FOR UPDATE SKIP LOCKED``; SQLite uses an immediate-transaction
        compare-and-set update. Returns the claimed job already transitioned
        to ``SCANNING``, or ``None`` when the queue is empty.
        """
        ...

    def begin_quarantine(
        self,
        job_id: str,
        *,
        reason: QuarantineReason,
        error_stack: str,
        failed_stage: str,
    ) -> QuarantineItem:
        """Atomically mark a document quarantined and create its active attempt."""
        ...

    def set_quarantine_locator(self, attempt_id: str, locator: str) -> None:
        """Attach the opaque storage locator after payload isolation."""
        ...

    def get_active_quarantine(self, document_id: str) -> QuarantineItem | None:
        """Return the unresolved quarantine attempt for a document."""
        ...

    def list_quarantine_items(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        reason: QuarantineReason | None = None,
    ) -> tuple[list[QuarantineItem], int]:
        """Page unresolved quarantine attempts newest first."""
        ...

    def reserve_redrive(
        self,
        document_id: str,
        *,
        actor: str,
        token: str,
    ) -> QuarantineItem | None:
        """CAS-reserve one active item; return None on conflict/ineligible state."""
        ...

    def finalize_redrive(
        self,
        document_id: str,
        *,
        actor: str,
        token: str,
    ) -> DocumentJob:
        """Atomically resolve the attempt and queue a clean, incremented job."""
        ...

    def emit_audit_event(
        self,
        *,
        job_id: str,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an immutable row to ``pipeline_events`` (non-repudiation)."""
        ...


@runtime_checkable
class LayoutExtractor(Protocol):
    """Structural layout extraction stage (Docling or compatible).

    Kept as a Protocol so ``core.pipeline`` never imports Docling or filesystem
    adapters — only this callable boundary.
    """

    def extract_layout(self, local_path: str) -> str:
        """Return markdown (or equivalent) layout for ``local_path``."""
        ...


@runtime_checkable
class VisionLLMClient(Protocol):
    """Pluggable vision LLM client (typically Ollama on a Mini PC)."""

    def extract_fields(
        self,
        local_path: str,
        layout_markdown: str,
    ) -> dict[str, Any]:
        """Run vision extraction against images + layout; return structured JSON."""
        ...
