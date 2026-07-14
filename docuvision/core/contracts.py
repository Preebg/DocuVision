"""Structural contracts for external backends (dependency inversion).

``DocumentPipeline`` depends only on these Protocols. Concrete adapters
(local filesystem, SQLite, Ollama HTTP client) live under ``docuvision.adapters``
and are wired at composition-root time.
"""

from __future__ import annotations

from typing import Any, BinaryIO, Protocol, Sequence, runtime_checkable

from docuvision.core.models import (
    Batch,
    BatchSummary,
    DocumentJob,
    DocumentPage,
    PipelineStatus,
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

    def write_incoming_batch(
        self,
        documents: Sequence[tuple[str, BinaryIO | bytes]],
    ) -> list[str]:
        """Write an ordered batch of documents into the ingress directory.

        Each item is ``(original_file_name, bytes_or_stream)``. Returns the
        stored basenames in the same sequence (may be uniquified on collision).
        """
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
