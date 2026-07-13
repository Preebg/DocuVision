"""Stage orchestration state machine — Protocol-only dependencies.

Happy-path transitions (each recorded via ``PipelineRepository``):

    QUEUED → DOWNLOADING → LAYOUT → VISION → STAGING → PENDING_VERIFICATION
"""

from __future__ import annotations

import logging

from docuvision.core.contracts import (
    LayoutExtractor,
    PipelineRepository,
    StorageBackend,
    VisionLLMClient,
)
from docuvision.core.models import DocumentJob, JobStatus, PipelineResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed pipeline exceptions
# ---------------------------------------------------------------------------


class PipelineError(Exception):
    """Base error for orchestration failures."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        file_name: str | None = None,
        status: JobStatus | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.file_name = file_name
        self.status = status
        self.cause = cause


class StorageError(PipelineError):
    """Ingress / staging / archive failure from ``StorageBackend``."""


class LayoutStageError(PipelineError):
    """Docling (or equivalent) layout extraction failure."""


class VisionStageError(PipelineError):
    """Vision LLM extraction failure."""


class StagingError(PipelineError):
    """Failure while staging a document for HITL verification."""


class RepositoryError(PipelineError):
    """Job persistence / state-transition failure."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class DocumentPipeline:
    """Runs one document through the explicit status state machine.

    Depends *only* on ``StorageBackend``, ``PipelineRepository``,
    ``LayoutExtractor``, and ``VisionLLMClient`` Protocols — never on concrete
    filesystem paths, SQLite, or Ollama URL details.
    """

    def __init__(
        self,
        storage: StorageBackend,
        repository: PipelineRepository,
        layout: LayoutExtractor,
        vision: VisionLLMClient,
        *,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._layout = layout
        self._vision = vision
        self._log = logger_ or logger

    def process(self, file_name: str) -> PipelineResult:
        """Execute the full happy-path (or record ``FAILED``) for ``file_name``."""
        job = self._repository.create_job(file_name)
        self._log.info(
            "pipeline.start job_id=%s file_name=%s status=%s",
            job.id,
            file_name,
            job.status.value,
        )

        local_path: str | None = None

        try:
            job, local_path = self._transition_downloading(job, file_name)
            job = self._transition_layout(job, local_path)
            job = self._transition_vision(job, local_path)
            job = self._transition_staging(job, file_name, local_path)
            job = self._transition_pending_verification(job)

            self._storage.mark_processed(file_name, local_path)
            self._log.info(
                "pipeline.success job_id=%s file_name=%s status=%s",
                job.id,
                file_name,
                job.status.value,
            )
            return PipelineResult(
                job_id=job.id,
                file_name=file_name,
                success=True,
                status=job.status,
                extracted_payload=job.extracted_payload,
            )

        except Exception as exc:
            return self._handle_failure(job, file_name, local_path, exc)

    def process_next(self) -> PipelineResult | None:
        """Claim the next incoming file (if any) and run ``process``."""
        incoming = self._storage.list_incoming()
        if not incoming:
            return None
        return self.process(incoming[0])

    # -- state transitions -------------------------------------------------

    def _transition_downloading(
        self, job: DocumentJob, file_name: str
    ) -> tuple[DocumentJob, str]:
        job = self._persist(job, JobStatus.DOWNLOADING)
        self._log.info("pipeline.state job_id=%s → %s", job.id, JobStatus.DOWNLOADING.value)
        try:
            local_path = self._storage.fetch_to_workspace(file_name)
        except Exception as exc:
            raise StorageError(
                f"Failed to fetch '{file_name}' into workspace: {exc}",
                job_id=job.id,
                file_name=file_name,
                status=JobStatus.DOWNLOADING,
                cause=exc,
            ) from exc
        job = self._persist(job, JobStatus.DOWNLOADING, local_path=local_path)
        return job, local_path

    def _transition_layout(self, job: DocumentJob, local_path: str) -> DocumentJob:
        job = self._persist(job, JobStatus.LAYOUT)
        self._log.info("pipeline.state job_id=%s → %s", job.id, JobStatus.LAYOUT.value)
        try:
            layout_markdown = self._layout.extract_layout(local_path)
        except Exception as exc:
            raise LayoutStageError(
                f"Layout extraction failed for '{job.file_name}': {exc}",
                job_id=job.id,
                file_name=job.file_name,
                status=JobStatus.LAYOUT,
                cause=exc,
            ) from exc
        return self._persist(job, JobStatus.LAYOUT, layout_markdown=layout_markdown)

    def _transition_vision(self, job: DocumentJob, local_path: str) -> DocumentJob:
        job = self._persist(job, JobStatus.VISION)
        self._log.info("pipeline.state job_id=%s → %s", job.id, JobStatus.VISION.value)
        layout_markdown = job.layout_markdown or ""
        try:
            payload = self._vision.extract_fields(local_path, layout_markdown)
        except Exception as exc:
            raise VisionStageError(
                f"Vision extraction failed for '{job.file_name}': {exc}",
                job_id=job.id,
                file_name=job.file_name,
                status=JobStatus.VISION,
                cause=exc,
            ) from exc
        return self._persist(job, JobStatus.VISION, extracted_payload=payload)

    def _transition_staging(
        self, job: DocumentJob, file_name: str, local_path: str
    ) -> DocumentJob:
        job = self._persist(job, JobStatus.STAGING)
        self._log.info("pipeline.state job_id=%s → %s", job.id, JobStatus.STAGING.value)
        try:
            self._storage.stage_for_verification(file_name, local_path)
        except Exception as exc:
            raise StagingError(
                f"Staging failed for '{file_name}': {exc}",
                job_id=job.id,
                file_name=file_name,
                status=JobStatus.STAGING,
                cause=exc,
            ) from exc
        return job

    def _transition_pending_verification(self, job: DocumentJob) -> DocumentJob:
        job = self._persist(job, JobStatus.PENDING_VERIFICATION)
        self._log.info(
            "pipeline.state job_id=%s → %s",
            job.id,
            JobStatus.PENDING_VERIFICATION.value,
        )
        return job

    # -- helpers -----------------------------------------------------------

    def _persist(
        self,
        job: DocumentJob,
        status: JobStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict | None = None,
        error_message: str | None = None,
    ) -> DocumentJob:
        try:
            return self._repository.update_status(
                job.id,
                status,
                local_path=local_path,
                layout_markdown=layout_markdown,
                extracted_payload=extracted_payload,
                error_message=error_message,
            )
        except Exception as exc:
            raise RepositoryError(
                f"Failed to persist status {status.value} for job {job.id}: {exc}",
                job_id=job.id,
                file_name=job.file_name,
                status=status,
                cause=exc,
            ) from exc

    def _handle_failure(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        exc: BaseException,
    ) -> PipelineResult:
        error_message = str(exc)
        self._log.error(
            "pipeline.failure job_id=%s file_name=%s error=%s",
            job.id,
            file_name,
            error_message,
        )

        try:
            failed = self._repository.mark_failed(job.id, error_message)
        except Exception as state_exc:
            self._log.error(
                "pipeline.state_error job_id=%s could not record FAILED: %s",
                job.id,
                state_exc,
            )
            failed = job.transition_to(JobStatus.FAILED, error_message=error_message)

        try:
            self._storage.mark_failed(file_name, local_path)
        except Exception as storage_exc:
            self._log.warning(
                "pipeline.storage_deadletter_error file_name=%s error=%s",
                file_name,
                storage_exc,
            )

        return PipelineResult(
            job_id=failed.id,
            file_name=file_name,
            success=False,
            status=JobStatus.FAILED,
            error_message=error_message,
        )
