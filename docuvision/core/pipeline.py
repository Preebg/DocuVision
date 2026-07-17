"""Stage orchestration state machine — Protocol-only dependencies.

Happy-path transitions (each recorded via ``PipelineRepository``):

    QUEUED → SCANNING → LAYOUT → VISION → STAGING → PENDING_VERIFICATION

Fault protection:

* Before extraction (LAYOUT), jobs with ``retry_count >= max_retries`` are
  isolated as ``QUARANTINED`` with ``metadata.dead_letter = true``.
* ``TransientError`` increments ``retry_count`` and reverts status to ``QUEUED``.
* Terminal wave exceptions are classified, traced, and quarantined.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

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
from docuvision.core.models import (
    DocumentJob,
    PipelineResult,
    PipelineStatus,
    QuarantineReason,
)
from docuvision.core.observability import bind_correlation, configure_json_logger

logger = configure_json_logger("docuvision.pipeline")
_T = TypeVar("_T")
_MAX_ERROR_STACK_CHARS = 32_000


class QuarantineBoundaryError(Exception):
    """Carries wave context and the original traceback to the outer boundary."""

    def __init__(self, stage: str, cause: BaseException, error_stack: str) -> None:
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause
        self.error_stack = error_stack[-_MAX_ERROR_STACK_CHARS:]


def execute_with_quarantine_boundary(
    stage: str,
    operation: Callable[[], _T],
) -> _T:
    """Execute one wave, preserving transient retries and isolating all else."""
    try:
        return operation()
    except TransientError:
        raise
    except BaseException as exc:
        raise QuarantineBoundaryError(stage, exc, traceback.format_exc()) from exc


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
        """Execute the full happy-path (or quarantine / re-queue)."""
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
        except QuarantineBoundaryError as exc:
            return self._quarantine_failure(
                job,
                file_name,
                local_path,
                exc.cause,
                job_log,
                correlation_id,
                failed_stage=exc.stage,
                error_stack=exc.error_stack,
            )
        except Exception as exc:
            return self._quarantine_failure(
                job,
                file_name,
                local_path,
                exc,
                job_log,
                correlation_id,
                failed_stage="ORCHESTRATION",
            )

    def process_next(self) -> PipelineResult | None:
        """Enroll ingress files, claim one ``QUEUED`` job, then run ``process``.

        Concurrent workers are safe: ``claim_next_queued`` uses
        ``FOR UPDATE SKIP LOCKED`` (Postgres) or an immediate CAS (SQLite),
        preferring documents in HIGH-priority batches.
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
        local_path = execute_with_quarantine_boundary(
            "SCANNING",
            lambda: self._storage.fetch_to_workspace(file_name),
        )
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
        layout_markdown = execute_with_quarantine_boundary(
            "LAYOUT",
            lambda: self._layout.extract_layout(local_path),
        )
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
        payload = execute_with_quarantine_boundary(
            "VISION",
            lambda: self._vision.extract_fields(local_path, layout_markdown),
        )
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
        execute_with_quarantine_boundary(
            "STAGING",
            lambda: self._storage.stage_for_verification(file_name, local_path),
        )
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
        # Batch domain: when every member is HITL-ready, advance to ready_for_hitl.
        try:
            advanced = self._repository.maybe_mark_batches_ready_for_hitl(job.id)
            for batch in advanced:
                job_log.info(
                    "pipeline.batch_ready_for_hitl batch_id=%s name=%s",
                    batch.id,
                    batch.name,
                )
        except Exception as exc:
            # Never fail document staging because of a batch status race.
            job_log.warning(
                "pipeline.batch_ready_for_hitl_failed document_id=%s error=%s",
                job.id,
                exc,
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
        saved = self._repository.save_job(job.model_copy(update={"metadata": metadata}))
        return self._quarantine_failure(
            saved,
            file_name,
            local_path,
            RuntimeError(error_message),
            job_log,
            correlation_id,
            failed_stage="RETRY",
            reason=QuarantineReason.RETRY_EXHAUSTED,
            error_stack=error_message,
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
            bumped = self._persist(
                job,
                job.status,
                job_log,
                retry_count=next_retry,
                error_message=str(exc),
            )
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

    @staticmethod
    def _classify_quarantine_reason(
        failed_stage: str,
        exc: BaseException,
    ) -> QuarantineReason:
        chain: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(chain) < 8:
            chain.append(f"{type(current).__name__}: {current}".casefold())
            current = current.__cause__ or current.__context__
        diagnostic = " ".join(chain)
        if any(token in diagnostic for token in ("out of memory", "vram", "cuda oom")):
            return QuarantineReason.VRAM_OOM
        if "timeout" in diagnostic or "timed out" in diagnostic:
            return QuarantineReason.NETWORK_TIMEOUT
        if failed_stage == "LAYOUT":
            return QuarantineReason.PARSING_ERROR
        if isinstance(exc, ValidationError) or "validation" in diagnostic or "schema" in diagnostic:
            return QuarantineReason.VALIDATION_FAILED
        if isinstance(exc, InfrastructureError):
            return QuarantineReason.INFRASTRUCTURE_ERROR
        return QuarantineReason.PROCESSING_ERROR

    def _quarantine_failure(
        self,
        job: DocumentJob,
        file_name: str,
        local_path: str | None,
        exc: BaseException,
        job_log: logging.LoggerAdapter[logging.Logger],
        correlation_id: str,
        *,
        failed_stage: str,
        reason: QuarantineReason | None = None,
        error_stack: str | None = None,
    ) -> PipelineResult:
        resolved_reason = reason or self._classify_quarantine_reason(
            failed_stage, exc
        )
        stack = (error_stack or traceback.format_exc())[-_MAX_ERROR_STACK_CHARS:]
        if stack.strip() == "NoneType: None":
            stack = f"{type(exc).__name__}: {exc}"
        job_log.error(
            "pipeline.quarantine file_name=%s stage=%s reason=%s error=%s",
            file_name,
            failed_stage,
            resolved_reason.value,
            exc,
        )
        try:
            item = self._repository.begin_quarantine(
                job.id,
                reason=resolved_reason,
                error_stack=stack,
                failed_stage=failed_stage,
            )
        except Exception as state_exc:
            job_log.critical(
                "pipeline.quarantine_persist_failed document_id=%s error=%s",
                job.id,
                state_exc,
            )
            # Preserve existing behavior as a final database fallback.
            failed = self._repository.mark_failed(job.id, str(exc))
            return PipelineResult(
                job_id=failed.id,
                file_name=file_name,
                success=False,
                status=failed.status,
                error_message=str(exc),
                correlation_id=correlation_id,
            )

        try:
            locator = self._storage.quarantine_payload(
                job.id,
                file_name,
                local_path,
            )
            self._repository.set_quarantine_locator(item.id, locator)
        except Exception as storage_exc:
            # The DB transition happens first: a crash/failure cannot cause the
            # worker to claim the document again. Redrive can recover from
            # incoming/failed/verification roots when no locator was recorded.
            job_log.exception(
                "pipeline.quarantine_storage_incomplete document_id=%s error=%s",
                job.id,
                storage_exc,
            )

        return PipelineResult(
            job_id=job.id,
            file_name=file_name,
            success=False,
            status=PipelineStatus.QUARANTINED,
            error_message=str(exc),
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
