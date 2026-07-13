"""Structural contracts for external backends (dependency inversion).

``DocumentPipeline`` depends only on these Protocols. Concrete adapters
(local filesystem, SQLite, Ollama HTTP client) live under ``docuvision.adapters``
and are wired at composition-root time.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from docuvision.core.models import DocumentJob, JobStatus


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


@runtime_checkable
class PipelineRepository(Protocol):
    """Durable job tracking and status persistence (e.g. embedded SQLite)."""

    def create_job(self, file_name: str) -> DocumentJob:
        """Insert a new job in ``QUEUED`` and return it."""
        ...

    def save_job(self, job: DocumentJob) -> DocumentJob:
        """Persist the full job snapshot (status + payloads)."""
        ...

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> DocumentJob:
        """Transition and persist a job's state."""
        ...

    def mark_failed(self, job_id: str, error_message: str) -> DocumentJob:
        """Record terminal ``FAILED`` status with diagnostic text."""
        ...

    def get_job(self, job_id: str) -> DocumentJob | None:
        """Load a job by id, or ``None`` if missing."""
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
