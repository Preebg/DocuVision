"""Tests for Migration 002 — batch domain schema, claim priority, and APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.api.main import create_app
from docuvision.config.settings import Settings
from docuvision.core.models import BatchStatus, PipelineStatus


def _api_client(tmp_path: Path) -> tuple[TestClient, SqlitePipelineRepository]:
    """Boot the FastAPI app and inject isolated SQLite + local storage."""
    settings = Settings(data_root=tmp_path / "data", database_url=None)
    storage = LocalStorageAdapter(settings)
    repository = SqlitePipelineRepository(tmp_path / "api.db", actor="test-api")
    app = create_app()
    client = TestClient(app)
    # Lifespan may have wired production backends; pin test doubles after enter.
    client.__enter__()
    app.state.settings = settings
    app.state.storage = storage
    app.state.repository = repository
    app.state.repository_kind = "sqlite"
    return client, repository


def test_migration_002_creates_batch_tables(tmp_path: Path) -> None:
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
    assert "batches" in tables
    assert "batch_documents" in tables
    assert "document_pages" in tables
    assert "idx_batches_status" in indexes
    assert "idx_batches_priority_created" in indexes
    assert "idx_document_pages_document_page" in indexes


def test_create_batch_links_documents_in_sequence(tmp_path: Path) -> None:
    repo = SqlitePipelineRepository(tmp_path / "dv.db")
    first = repo.create_job("page_a.pdf")
    second = repo.create_job("page_b.pdf")
    batch = repo.create_batch(
        name="NYSDOH_Ingest_2026_07_14",
        priority=3,
        source_id="/scans/nysdoh",
        documents=[(first.id, 1), (second.id, 2)],
    )
    assert batch.status == BatchStatus.INGESTED
    assert batch.priority == 3
    assert [link.sequence_no for link in batch.documents] == [1, 2]
    assert [link.file_name for link in batch.documents] == ["page_a.pdf", "page_b.pdf"]

    pending = repo.list_pending_batches()
    assert len(pending) == 1
    assert pending[0].document_count == 2
    assert pending[0].name == "NYSDOH_Ingest_2026_07_14"


def test_claim_next_queued_prefers_high_priority_batch(tmp_path: Path) -> None:
    repo = SqlitePipelineRepository(tmp_path / "dv.db")
    low = repo.create_job("low.pdf")
    high = repo.create_job("high.pdf")
    medium = repo.create_job("standalone.pdf")

    older = datetime.now(timezone.utc) - timedelta(hours=2)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE documents SET created_at = ? WHERE id = ?",
            (older.isoformat(), low.id),
        )

    repo.create_batch(
        name="low_batch",
        priority=1,
        documents=[(low.id, 1)],
    )
    repo.create_batch(
        name="high_batch",
        priority=3,
        documents=[(high.id, 1)],
    )

    claimed = repo.claim_next_queued()
    assert claimed is not None
    assert claimed.id == high.id
    assert claimed.status == PipelineStatus.SCANNING

    pending = {item.name: item for item in repo.list_pending_batches()}
    assert pending["high_batch"].status == BatchStatus.PROCESSING

    claimed_second = repo.claim_next_queued()
    assert claimed_second is not None
    # Standalone (default priority 2) beats low-priority batch docs.
    assert claimed_second.id == medium.id


def test_add_document_pages(tmp_path: Path) -> None:
    repo = SqlitePipelineRepository(tmp_path / "dv.db")
    job = repo.create_job("packet.pdf")
    pages = repo.add_document_pages(
        job.id,
        [(1, "/pages/packet_001.png"), (2, "/pages/packet_002.png")],
    )
    assert [page.page_no for page in pages] == [1, 2]
    with repo._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM document_pages WHERE document_id = ?",
            (job.id,),
        ).fetchone()[0]
    assert count == 2


def test_batch_ingest_and_pending_endpoints(tmp_path: Path) -> None:
    client, _repository = _api_client(tmp_path)
    try:
        response = client.post(
            "/api/batches/ingest",
            data={
                "name": "NYSDOH_Ingest_2026_07_14",
                "priority": "3",
                "source_id": "folder://nysdoh/inbox",
            },
            files=[
                ("files", ("app_cover.pdf", b"%PDF-1.4 cover", "application/pdf")),
                ("files", ("app_form.pdf", b"%PDF-1.4 form", "application/pdf")),
            ],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "NYSDOH_Ingest_2026_07_14"
        assert body["priority"] == 3
        assert body["status"] == "ingested"
        assert len(body["documents"]) == 2
        assert body["documents"][0]["sequence_no"] == 1
        assert body["documents"][1]["sequence_no"] == 2

        pending = client.get("/api/batches/pending")
        assert pending.status_code == 200
        batches = pending.json()["batches"]
        assert len(batches) == 1
        assert batches[0]["document_count"] == 2
        assert batches[0]["priority"] == 3
    finally:
        client.__exit__(None, None, None)


def test_batch_ingest_rejects_bad_priority(tmp_path: Path) -> None:
    client, _repository = _api_client(tmp_path)
    try:
        response = client.post(
            "/api/batches/ingest",
            data={"name": "bad", "priority": "9"},
            files=[("files", ("x.pdf", b"%PDF-1.4", "application/pdf"))],
        )
        assert response.status_code == 400
    finally:
        client.__exit__(None, None, None)
