"""Tests for Pillar 5 — ReleaseExporter / FSDropExporter / batch release FSM."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.release import FSDropExporter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.api.main import create_app
from docuvision.composition import build_release_engine
from docuvision.config.settings import Settings
from docuvision.core.errors import ReleaseExportError
from docuvision.core.models import BatchStatus, PipelineStatus
from docuvision.core.release import ReleaseEngine


def _minimal_pdf_bytes(text: str = "DocuVision") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _write_pdf(path: Path, text: str = "DocuVision") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_minimal_pdf_bytes(text))
    return path


@pytest.mark.asyncio
async def test_fs_drop_exporter_writes_package_and_checksums(tmp_path: Path) -> None:
    release_root = tmp_path / "release_share"
    source = _write_pdf(tmp_path / "src" / "form.pdf", "Form A")
    exporter = FSDropExporter(release_root)

    payload = {
        "batch": {
            "id": "batch-1",
            "name": "NYSDOH_Ingest_2026_07_14",
            "priority": 3,
            "source_id": "folder://inbox",
        },
        "documents": [
            {
                "document_id": "doc-1",
                "sequence_no": 1,
                "file_name": "form.pdf",
                "source_path": str(source),
                "extracted_payload": {"fields": {"case_id": "AI-1"}},
                "verified_payload": {"fields": {"case_id": "CLERK-1"}},
                "verified_by": "clerk.jdoe",
                "fields": {"case_id": "CLERK-1"},
            }
        ],
    }
    ok = await exporter.export_batch("batch-1", payload)
    assert ok is True

    package = release_root / "NYSDOH_Ingest_2026_07_14_batch-1"
    assert package.is_dir()
    pdfs = list(package.glob("*.pdf"))
    assert len(pdfs) == 1
    assert pdfs[0].name.startswith("01_")

    metadata = json.loads((package / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["batch_id"] == "batch-1"
    assert metadata["documents"][0]["fields"]["case_id"] == "CLERK-1"
    assert metadata["documents"][0]["verified_by"] == "clerk.jdoe"

    manifest = (package / "checksums.sha256").read_text(encoding="utf-8").strip().splitlines()
    assert len(manifest) == 2  # pdf + metadata.json
    for line in manifest:
        digest, _, relative = line.partition("  ")
        assert len(digest) == 64
        actual = hashlib.sha256((package / relative).read_bytes()).hexdigest()
        assert digest == actual


@pytest.mark.asyncio
async def test_fs_drop_exporter_raises_on_mount_failure(tmp_path: Path) -> None:
    exporter = FSDropExporter(tmp_path / "release")
    with pytest.raises(ReleaseExportError):
        await exporter.export_batch(
            "missing",
            {
                "batch": {"name": "x"},
                "documents": [
                    {
                        "document_id": "d1",
                        "file_name": "gone.pdf",
                        "source_path": str(tmp_path / "nope.pdf"),
                    }
                ],
            },
        )


@pytest.mark.asyncio
async def test_release_engine_status_machine_success(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        release_dir=tmp_path / "share",
        database_url=None,
    )
    storage = LocalStorageAdapter(settings)
    repo = SqlitePipelineRepository(tmp_path / "dv.db", actor="test")

    pdf_name = storage.write_incoming("app.pdf", _minimal_pdf_bytes("A"))
    # Stage as verification copy so resolve_document_path finds it.
    storage.stage_for_verification(
        pdf_name,
        str(settings.resolved_incoming_dir() / pdf_name),
    )

    job = repo.create_job(pdf_name)
    batch = repo.create_batch(
        name="Release_Packet",
        priority=3,
        documents=[(job.id, 1)],
    )
    repo.update_status(
        job.id,
        PipelineStatus.PENDING_VERIFICATION,
        extracted_payload={"fields": {"a": 1}},
    )
    repo.update_batch_status(batch.id, BatchStatus.PROCESSING)
    advanced = repo.maybe_mark_batches_ready_for_hitl(job.id)
    assert advanced
    assert advanced[0].status == BatchStatus.READY_FOR_HITL

    repo.update_status(
        job.id,
        PipelineStatus.VERIFIED,
        metadata={"verified_payload": {"fields": {"a": 2}}, "verified_by": "clerk"},
    )

    engine = build_release_engine(settings, storage, repo)
    released = await engine.release_batch(batch.id, actor="clerk")
    assert released.status == BatchStatus.RELEASED

    package = tmp_path / "share" / f"Release_Packet_{batch.id}"
    assert (package / "metadata.json").is_file()
    assert (package / "checksums.sha256").is_file()
    assert list(package.glob("*.pdf"))


@pytest.mark.asyncio
async def test_release_engine_marks_release_failed_without_raising(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        release_dir=tmp_path / "share",
        database_url=None,
    )
    storage = LocalStorageAdapter(settings)
    repo = SqlitePipelineRepository(tmp_path / "dv.db", actor="test")
    job = repo.create_job("missing.pdf")
    batch = repo.create_batch(name="Fail_Packet", documents=[(job.id, 1)])
    repo.update_status(job.id, PipelineStatus.VERIFIED, metadata={"verified_by": "c"})
    repo.update_batch_status(batch.id, BatchStatus.READY_FOR_HITL)

    engine = build_release_engine(settings, storage, repo)
    result = await engine.release_batch(batch.id, actor="clerk")
    assert result.status == BatchStatus.RELEASE_FAILED

    # Clerk verification is untouched — retries allowed.
    still = repo.get_job(job.id)
    assert still is not None
    assert still.status == PipelineStatus.VERIFIED


def test_approve_and_release_api_hook(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        release_dir=tmp_path / "share",
        database_url=None,
    )
    storage = LocalStorageAdapter(settings)
    repository = SqlitePipelineRepository(tmp_path / "api.db", actor="test-api")
    exporter = FSDropExporter(settings.resolved_release_dir())
    engine = ReleaseEngine(repository, storage, exporter)

    pdf_a = storage.write_incoming("a.pdf", _minimal_pdf_bytes("A"))
    pdf_b = storage.write_incoming("b.pdf", _minimal_pdf_bytes("B"))
    storage.stage_for_verification(pdf_a, str(settings.resolved_incoming_dir() / pdf_a))
    storage.stage_for_verification(pdf_b, str(settings.resolved_incoming_dir() / pdf_b))

    job_a = repository.create_job(pdf_a)
    job_b = repository.create_job(pdf_b)
    batch = repository.create_batch(
        name="API_Batch",
        priority=2,
        documents=[(job_a.id, 1), (job_b.id, 2)],
    )
    for job in (job_a, job_b):
        repository.update_status(
            job.id,
            PipelineStatus.PENDING_VERIFICATION,
            extracted_payload={"fields": {"x": "ai"}},
        )
    repository.update_batch_status(batch.id, BatchStatus.PROCESSING)
    repository.maybe_mark_batches_ready_for_hitl(job_b.id)
    assert repository.get_batch(batch.id).status == BatchStatus.READY_FOR_HITL

    app = create_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.settings = settings
        app.state.storage = storage
        app.state.repository = repository
        app.state.release_exporter = exporter
        app.state.release_engine = engine
        app.state.repository_kind = "sqlite"

        first = client.patch(
            f"/api/documents/{job_a.id}",
            json={
                "action": "approve",
                "verified_payload": {"fields": {"x": "ok"}},
                "verified_by": "clerk.ada",
            },
            headers={"X-Operator-Identity": "clerk.ada"},
        )
        assert first.status_code == 200, first.text
        assert repository.get_batch(batch.id).status == BatchStatus.READY_FOR_HITL

        second = client.patch(
            f"/api/documents/{job_b.id}",
            json={
                "action": "approve",
                "verified_payload": {"fields": {"x": "ok"}},
                "verified_by": "clerk.ada",
            },
            headers={"X-Operator-Identity": "clerk.ada"},
        )
        assert second.status_code == 200, second.text
        assert repository.get_batch(batch.id).status == BatchStatus.RELEASED
        package = tmp_path / "share" / f"API_Batch_{batch.id}"
        assert (package / "checksums.sha256").is_file()
        assert len(list(package.glob("*.pdf"))) == 2
    finally:
        client.__exit__(None, None, None)
