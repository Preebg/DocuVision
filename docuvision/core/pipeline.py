"""Stage orchestration state machine — Protocol-only dependencies.

Happy-path transitions (each recorded via ``PipelineRepository``):

    QUEUED → SCANNING → LAYOUT → VISION → STAGING → PENDING_VERIFICATION

Fault protection:

* Before extraction (LAYOUT), jobs with ``retry_count >= max_retries`` are
  dead-lettered to ``FAILED`` with ``metadata.dead_letter = true``.
* ``TransientError`` increments ``retry_count`` and reverts status to ``QUEUED``.
* ``ValidationError`` / ``FatalProcessingError`` fail immediately (no retry).
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

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
        max_retries: int = 3,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._layout = layout
        self._vision = vision
        self._max_retries = max_retries
        self._log = logger_ or logger

    def process(self, file_name: str) -> PipelineResult:
        """Execute the full happy-path (or record ``FAILED`` / re-queue)."""
        job = self._repository.create_job(file_name)
        correlation_id = job.id
        job_log = bind_correlation(self._log, correlation_id)
        job_log.info(
            "pipeline.start file_name=%s status=%s retry_count=%s",
            file_name,
            job.status.value,
            job.retry_count,
        )

        local_path: str | None = None

        try:
            if job.retry_count >= self._max_retries:
                return self._dead_letter(
                    job, file_name, local_path, job_log, correlation_id
                )

            job, local_path = self._transition_scanning(job, file_name, job_log)

            # Re-check after scan in case the row was mutated; extraction gate.
            if job.retry_count >= self._max_retries:
                return self._dead_letter(
                    job, file_name, local_path, job_log, correlation_id
                )

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

        except TransientError as exc:
            return self._handle_transient(
                job, file_name, local_path, exc, job_log, correlation_id
            )
        except (ValidationError, FatalProcessingError) as exc:
            return self._handle_non_retryable(
                job, file_name, local_path, exc, job_log, correlation_id
            )
        except Exception as exc:
            return self._handle_failure(
                job, file_name, local_path, exc, job_log, correlation_id
            )

    def process_next(self) -> PipelineResult | None:
        """Enroll ingress files, claim one ``QUEUED`` job, then run ``process``.

        Concurrent workers are safe: ``claim_next_queued`` uses
        ``FOR UPDATE SKIP LOCKED`` (Postgres) or an immediate CAS (SQLite).
        """
        for file_name in self._storage.list_incoming():
            self._repository.create_job(file_name)

        claimed = self._repository.claim_next_queued()
        if claimed is None:
            return None
        return self.process(claimed.file_name)

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
        except TransientError:
            raise
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
        except TransientError:
            raise
        except ValidationError:
            raise
        except FatalProcessingError:
            raise
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
        except TransientError:
            raise
        except ValidationError:
            raise
        except FatalProcessingError:
            raise
        except Exception as exc:
            # Prefer taxonomy mapping: schema/corrupt → ValidationError.
            if exc.__class__.__name__ == "ValidationError":
                raise ValidationError(
                    f"Vision extraction validation failed for '{job.file_name}': {exc}",
                    correlation_id=job.id,
                ) from exc
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
        except TransientError:
            raise
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
        retry_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentJob:
        try:
            return self._repository.update_status(
                job.id,
                status,
                local_path=local_path,
                layout_markdown=layout_markdown,
                extracted_payload=extracted_payload,
                error_message=error_message,
                retry_count=retry_count,
                metadata=metadata,
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

    def _dead_letter(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        job_log: logging.LoggerAdapter[logging.Logger],
        correlation_id: str,
    ) -> PipelineResult:
        error_message = (
            f"Dead-lettered after {job.retry_count} retries "
            f"(max={self._max_retries})"
        )
        metadata = {
            **job.metadata,
            "dead_letter": True,
            "retry_count": job.retry_count,
            "max_retries": self._max_retries,
        }
        job_log.critical(
            "pipeline.dead_letter file_name=%s retry_count=%s max_retries=%s",
            file_name,
            job.retry_count,
            self._max_retries,
        )
        try:
            failed = self._persist(
                job,
                PipelineStatus.FAILED,
                job_log,
                error_message=error_message,
                retry_count=job.retry_count,
                metadata=metadata,
            )
        except Exception as state_exc:
            job_log.error(
                "pipeline.state_error could not record dead-letter FAILED: %s",
                state_exc,
            )
            failed = job.transition_to(
                PipelineStatus.FAILED,
                error_message=error_message,
                retry_count=job.retry_count,
                metadata=metadata,
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

    def _handle_transient(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        exc: TransientError,
        job_log: logging.LoggerAdapter[logging.Logger],
        correlation_id: str,
    ) -> PipelineResult:
        next_retry = job.retry_count + 1
        job_log.warning(
            "pipeline.transient_retry file_name=%s correlation_id=%s "
            "retry_count=%s→%s error=%s",
            file_name,
            correlation_id,
            job.retry_count,
            next_retry,
            exc,
        )

        if next_retry >= self._max_retries:
            bumped = job.model_copy(update={"retry_count": next_retry})
            return self._dead_letter(
                bumped, file_name, local_path, job_log, correlation_id
            )

        metadata = {
            **job.metadata,
            "last_transient_error": str(exc),
            "last_transient_traceback": traceback.format_exc(),
        }
        try:
            requeued = self._persist(
                job,
                PipelineStatus.QUEUED,
                job_log,
                retry_count=next_retry,
                metadata=metadata,
                error_message=str(exc),
            )
        except Exception as state_exc:
            job_log.error(
                "pipeline.state_error could not re-queue after transient: %s",
                state_exc,
            )
            requeued = job.transition_to(
                PipelineStatus.QUEUED,
                retry_count=next_retry,
                metadata=metadata,
                error_message=str(exc),
            )

        return PipelineResult(
            job_id=requeued.id,
            file_name=file_name,
            success=False,
            status=PipelineStatus.QUEUED,
            error_message=str(exc),
            correlation_id=correlation_id,
        )

    def _handle_non_retryable(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        exc: ValidationError | FatalProcessingError,
        job_log: logging.LoggerAdapter[logging.Logger],
        correlation_id: str,
    ) -> PipelineResult:
        error_message = str(exc)
        metadata = {
            **job.metadata,
            "validation_failures": [error_message],
            "error_class": type(exc).__name__,
            "error_traceback": traceback.format_exc(),
            "dead_letter": False,
        }
        job_log.critical(
            "pipeline.non_retryable_failure file_name=%s error_class=%s error=%s",
            file_name,
            type(exc).__name__,
            error_message,
        )

        try:
            failed = self._persist(
                job,
                PipelineStatus.FAILED,
                job_log,
                error_message=error_message,
                metadata=metadata,
            )
        except Exception as state_exc:
            job_log.error(
                "pipeline.state_error could not record FAILED: %s",
                state_exc,
            )
            failed = job.transition_to(
                PipelineStatus.FAILED,
                error_message=error_message,
                metadata=metadata,
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
