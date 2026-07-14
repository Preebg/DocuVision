"""Unit tests for pipeline fault protection and SQLite documents schema."""

from __future__ import annotations

from typing import Any

from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.core.errors import FatalProcessingError, TransientError, ValidationError
from docuvision.core.models import PipelineStatus
from docuvision.core.pipeline import DocumentPipeline


class _FakeStorage:
    def __init__(self) -> None:
        self.incoming = ["form.pdf"]
        self.failed: list[tuple[str, str | None]] = []
        self.processed: list[tuple[str, str]] = []

    def list_incoming(self) -> list[str]:
        return list(self.incoming)

    def fetch_to_workspace(self, file_name: str) -> str:
        return f"/workspace/{file_name}"

    def stage_for_verification(self, file_name: str, local_path: str) -> str:
        return f"/verification/{file_name}"

    def mark_processed(self, file_name: str, local_path: str) -> None:
        self.processed.append((file_name, local_path))

    def mark_failed(self, file_name: str, local_path: str | None) -> None:
        self.failed.append((file_name, local_path))


class _FakeLayout:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def extract_layout(self, local_path: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "# layout"


class _FakeVision:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def extract_fields(self, local_path: str, layout_markdown: str) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "patient_name": "A",
            "dob": "2000-01-01",
            "form_type": "intake",
            "extracted_payload": {
                "full_text": "x",
                "fields": {},
                "summary": "y",
            },
        }


def _build(
    tmp_path,
    *,
    layout: _FakeLayout | None = None,
    vision: _FakeVision | None = None,
    max_retries: int = 3,
) -> tuple[DocumentPipeline, SqlitePipelineRepository, _FakeStorage]:
    storage = _FakeStorage()
    repo = SqlitePipelineRepository(tmp_path / "jobs.db", actor="test-worker")
    pipeline = DocumentPipeline(
        storage=storage,
        repository=repo,
        layout=layout or _FakeLayout(),
        vision=vision or _FakeVision(),
        max_retries=max_retries,
    )
    return pipeline, repo, storage


def test_migration_creates_documents_and_pipeline_events(tmp_path):
    repo = SqlitePipelineRepository(tmp_path / "dv.db")
    with repo._connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "documents" in tables
    assert "pipeline_events" in tables
    assert "batches" in tables
    assert "batch_documents" in tables
    assert "document_pages" in tables
    assert "idx_pipeline_events_job_id" in indexes
    assert "idx_pipeline_events_created_at" in indexes
    assert "idx_batches_status" in indexes

def test_happy_path_reaches_pending_verification(tmp_path):
    pipeline, repo, storage = _build(tmp_path)
    result = pipeline.process("form.pdf")
    assert result.success is True
    assert result.status == PipelineStatus.PENDING_VERIFICATION
    job = repo.get_job(result.job_id)
    assert job is not None
    assert job.status == PipelineStatus.PENDING_VERIFICATION
    assert storage.processed


def test_transient_error_increments_retry_and_requeues(tmp_path):
    layout = _FakeLayout(error=TransientError("ollama timeout"))
    pipeline, repo, storage = _build(tmp_path, layout=layout, max_retries=3)
    result = pipeline.process("form.pdf")

    assert result.success is False
    assert result.status == PipelineStatus.QUEUED
    job = repo.get_job(result.job_id)
    assert job is not None
    assert job.retry_count == 1
    assert job.status == PipelineStatus.QUEUED
    assert not storage.failed


def test_validation_error_fails_without_retry(tmp_path):
    layout = _FakeLayout(error=ValidationError("corrupt PDF"))
    pipeline, repo, storage = _build(tmp_path, layout=layout)
    result = pipeline.process("form.pdf")

    assert result.success is False
    assert result.status == PipelineStatus.FAILED
    job = repo.get_job(result.job_id)
    assert job is not None
    assert job.retry_count == 0
    assert job.metadata.get("validation_failures") == ["corrupt PDF"]
    assert storage.failed


def test_fatal_processing_error_fails_without_retry(tmp_path):
    vision = _FakeVision(error=FatalProcessingError("model crashed"))
    pipeline, repo, storage = _build(tmp_path, vision=vision)
    result = pipeline.process("form.pdf")

    assert result.success is False
    assert result.status == PipelineStatus.FAILED
    job = repo.get_job(result.job_id)
    assert job is not None
    assert "model crashed" in (job.metadata.get("validation_failures") or [""])[0]
    assert storage.failed


def test_dead_letter_when_retry_count_at_ceiling(tmp_path):
    pipeline, repo, storage = _build(tmp_path, max_retries=2)
    job = repo.create_job("form.pdf")
    repo.save_job(job.model_copy(update={"retry_count": 2}))

    result = pipeline.process("form.pdf")

    assert result.success is False
    assert result.status == PipelineStatus.FAILED
    failed = repo.get_job(result.job_id)
    assert failed is not None
    assert failed.metadata.get("dead_letter") is True
    assert storage.failed


def test_transient_at_max_retries_dead_letters(tmp_path):
    layout = _FakeLayout(error=TransientError("flaky network"))
    pipeline, repo, storage = _build(tmp_path, layout=layout, max_retries=1)
    # Seed retry_count so next_retry (==1) hits the ceiling.
    job = repo.create_job("form.pdf")
    repo.save_job(job.model_copy(update={"retry_count": 0}))

    result = pipeline.process("form.pdf")

    assert result.status == PipelineStatus.FAILED
    failed = repo.get_job(result.job_id)
    assert failed is not None
    assert failed.metadata.get("dead_letter") is True
    assert failed.retry_count == 1
    assert storage.failed
