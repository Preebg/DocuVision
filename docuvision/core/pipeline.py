"""Stage orchestration state machine — Protocol-only dependencies.

Happy-path transitions (each recorded via ``PipelineRepository``):

    QUEUED → SCANNING → LAYOUT → VISION → STAGING → PENDING_VERIFICATION
"""

from __future__ import annotations

import logging

from docuvision.core.contracts import (
    LayoutExtractor,
    PipelineRepository,
    StorageBackend,
    VisionLLMClient,
)
from docuvision.core.errors import (
    FatalProcessingError,
    InfrastructureError,
    PipelineError,
    TransientError,
    ValidationError,
)
from docuvision.core.models import DocumentJob, PipelineResult, PipelineStatus
from docuvision.core.observability import bind_correlation, configure_json_logger

logger = configure_json_logger("docuvision.pipeline")


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
        correlation_id = job.id
        job_log = bind_correlation(self._log, correlation_id)
        job_log.info(
            "pipeline.start file_name=%s status=%s",
            file_name,
            job.status.value,
        )

        local_path: str | None = None

        try:
            job, local_path = self._transition_scanning(job, file_name, job_log)
            job = self._transition_layout(job, local_path, job_log)
            job = self._transition_vision(job, local_path, job_log)
            job = self._transition_staging(job, file_name, local_path, job_log)
            job = self._transition_pending_verification(job, job_log)

            self._storage.mark_processed(file_name, local_path)
            job_log.info(
                "pipeline.success file_name=%s status=%s",
                file_name,
                job.status.value,
            )
            return PipelineResult(
                job_id=job.id,
                file_name=file_name,
                success=True,
                status=job.status,
                extracted_payload=job.extracted_payload,
                correlation_id=correlation_id,
            )

        except Exception as exc:
            return self._handle_failure(
                job, file_name, local_path, exc, job_log, correlation_id
            )

    def process_next(self) -> PipelineResult | None:
        """Claim the next incoming file (if any) and run ``process``."""
        incoming = self._storage.list_incoming()
        if not incoming:
            return None
        return self.process(incoming[0])

    # -- state transitions -------------------------------------------------

    def _transition_scanning(
        self,
        job: DocumentJob,
        file_name: str,
        job_log: logging.LoggerAdapter[logging.Logger],
    ) -> tuple[DocumentJob, str]:
        job = self._persist(job, PipelineStatus.SCANNING, job_log)
        job_log.info("pipeline.state → %s", PipelineStatus.SCANNING.value)
        try:
            local_path = self._storage.fetch_to_workspace(file_name)
        except Exception as exc:
            raise TransientError(
                f"Failed to fetch '{file_name}' into workspace: {exc}",
                correlation_id=job.id,
            ) from exc
        job = self._persist(
            job, PipelineStatus.SCANNING, job_log, local_path=local_path
        )
        return job, local_path

    def _transition_layout(
        self,
        job: DocumentJob,
        local_path: str,
        job_log: logging.LoggerAdapter[logging.Logger],
    ) -> DocumentJob:
        job = self._persist(job, PipelineStatus.LAYOUT, job_log)
        job_log.info("pipeline.state → %s", PipelineStatus.LAYOUT.value)
        try:
            layout_markdown = self._layout.extract_layout(local_path)
        except Exception as exc:
            raise ValidationError(
                f"Layout extraction failed for '{job.file_name}': {exc}",
                correlation_id=job.id,
            ) from exc
        return self._persist(
            job, PipelineStatus.LAYOUT, job_log, layout_markdown=layout_markdown
        )

    def _transition_vision(
        self,
        job: DocumentJob,
        local_path: str,
        job_log: logging.LoggerAdapter[logging.Logger],
    ) -> DocumentJob:
        job = self._persist(job, PipelineStatus.VISION, job_log)
        job_log.info("pipeline.state → %s", PipelineStatus.VISION.value)
        layout_markdown = job.layout_markdown or ""
        try:
            payload = self._vision.extract_fields(local_path, layout_markdown)
        except Exception as exc:
            raise FatalProcessingError(
                f"Vision extraction failed for '{job.file_name}': {exc}",
                correlation_id=job.id,
            ) from exc
        return self._persist(
            job, PipelineStatus.VISION, job_log, extracted_payload=payload
        )

    def _transition_staging(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str,
        job_log: logging.LoggerAdapter[logging.Logger],
    ) -> DocumentJob:
        job = self._persist(job, PipelineStatus.STAGING, job_log)
        job_log.info("pipeline.state → %s", PipelineStatus.STAGING.value)
        try:
            self._storage.stage_for_verification(file_name, local_path)
        except Exception as exc:
            raise InfrastructureError(
                f"Staging failed for '{file_name}': {exc}",
                correlation_id=job.id,
            ) from exc
        return job

    def _transition_pending_verification(
        self,
        job: DocumentJob,
        job_log: logging.LoggerAdapter[logging.Logger],
    ) -> DocumentJob:
        job = self._persist(job, PipelineStatus.PENDING_VERIFICATION, job_log)
        job_log.info(
            "pipeline.state → %s",
            PipelineStatus.PENDING_VERIFICATION.value,
        )
        return job

    # -- helpers -----------------------------------------------------------

    def _persist(
        self,
        job: DocumentJob,
        status: PipelineStatus,
        job_log: logging.LoggerAdapter[logging.Logger],
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
            job_log.error(
                "pipeline.persist_failed status=%s error=%s",
                status.value,
                exc,
            )
            raise InfrastructureError(
                f"Failed to persist status {status.value} for job {job.id}: {exc}",
                correlation_id=job.id,
            ) from exc

    def _handle_failure(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        exc: BaseException,
        job_log: logging.LoggerAdapter[logging.Logger],
        correlation_id: str,
    ) -> PipelineResult:
        error_message = str(exc)
        error_class = type(exc).__name__
        job_log.error(
            "pipeline.failure file_name=%s error_class=%s error=%s",
            file_name,
            error_class,
            error_message,
        )

        try:
            failed = self._repository.mark_failed(job.id, error_message)
        except Exception as state_exc:
            job_log.error(
                "pipeline.state_error could not record FAILED: %s",
                state_exc,
            )
            failed = job.transition_to(
                PipelineStatus.FAILED, error_message=error_message
            )

        try:
            self._storage.mark_failed(file_name, local_path)
        except Exception as storage_exc:
            job_log.warning(
                "pipeline.storage_deadletter_error error=%s",
                storage_exc,
            )

        return PipelineResult(
            job_id=failed.id,
            file_name=file_name,
            success=False,
            status=PipelineStatus.FAILED,
            error_message=error_message,
            correlation_id=correlation_id,
        )


__all__ = [
    "DocumentPipeline",
    "FatalProcessingError",
    "InfrastructureError",
    "PipelineError",
    "TransientError",
    "ValidationError",
]
