"""Tests for Pillar 1 — local ingestion watcher, packager, and status API."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from conftest import LOCAL_API_HEADERS
from docuvision.adapters.ingestion.config import load_ingestion_sources
from docuvision.adapters.ingestion.packager import BatchPackager, group_key_for_path
from docuvision.adapters.ingestion.status_store import (
    IngestionStatusStore,
    SourceRuntimeStatus,
)
from docuvision.adapters.ingestion.watcher import IngestionWatcher
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.api.main import create_app
from docuvision.config.settings import Settings
from docuvision.core.models import BatchStatus, PipelineStatus


def _write_sources_yaml(path: Path, sources: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"sources": sources}), encoding="utf-8")
    return path


def test_resolve_data_prefix_against_data_root(tmp_path: Path) -> None:
    from docuvision.adapters.ingestion.config import resolve_configured_path

    root = tmp_path / "data"
    resolved = resolve_configured_path("/data/drops/scanner_a", root)
    assert resolved == (root / "drops" / "scanner_a").resolve()
    relative = resolve_configured_path("quarantine/scanner_a", root)
    assert relative == (root / "quarantine" / "scanner_a").resolve()


def test_load_ingestion_sources_validates(tmp_path: Path) -> None:
    yaml_path = _write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            {
                "source_id": "scanner_a",
                "display_name": "Scanner A",
                "type": "local_directory",
                "path": str(tmp_path / "drop"),
                "batch_priority": 3,
                "polling_interval_seconds": 2,
                "file_filters": [".pdf", ".tiff"],
                "quarantine_path": str(tmp_path / "quarantine"),
                "batch_window_seconds": 1,
            }
        ],
    )
    loaded = load_ingestion_sources(yaml_path)
    assert len(loaded) == 1
    assert loaded[0].source_id == "scanner_a"
    assert loaded[0].batch_priority == 3
    assert loaded[0].matches(Path("form.PDF"))
    assert not loaded[0].matches(Path("notes.txt"))


def test_group_key_uses_subfolder(tmp_path: Path) -> None:
    from docuvision.adapters.ingestion.config import IngestionSourceConfig

    root = tmp_path / "drop"
    source = IngestionSourceConfig(
        source_id="s1",
        display_name="S1",
        type="local_directory",
        path=root,
        batch_priority=2,
        polling_interval_seconds=5,
        file_filters=(".pdf",),
        quarantine_path=tmp_path / "q",
    )
    flat = root / "a.pdf"
    nested = root / "packet_01" / "b.pdf"
    nested.parent.mkdir(parents=True)
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"%PDF-1.4")
    nested.write_bytes(b"%PDF-1.4")
    assert group_key_for_path(source, flat) == "__root__"
    assert group_key_for_path(source, nested) == "packet_01"


def test_packager_writes_batch_and_archives(tmp_path: Path) -> None:
    from docuvision.adapters.ingestion.config import IngestionSourceConfig

    settings = Settings(data_root=tmp_path / "data", database_url=None)
    storage = LocalStorageAdapter(settings)
    repo = SqlitePipelineRepository(tmp_path / "dv.db", actor="test")
    packager = BatchPackager(
        storage,
        repo,
        archive_root=tmp_path / "archive",
    )

    drop = tmp_path / "drop"
    drop.mkdir()
    quarantine = tmp_path / "quarantine"
    source = IngestionSourceConfig(
        source_id="scanner_local",
        display_name="Local",
        type="local_directory",
        path=drop,
        batch_priority=3,
        polling_interval_seconds=1,
        file_filters=(".pdf",),
        quarantine_path=quarantine,
    )
    f1 = drop / "page1.pdf"
    f2 = drop / "page2.pdf"
    f1.write_bytes(b"%PDF-fake-1")
    f2.write_bytes(b"%PDF-fake-2")

    batch_id = packager.ingest_group(source, [f1, f2], group_key="__root__")
    assert batch_id is not None
    assert not f1.exists()
    assert not f2.exists()
    assert storage.list_incoming() == ["page1.pdf", "page2.pdf"]

    batch = repo.get_batch(batch_id)
    assert batch is not None
    assert batch.status == BatchStatus.INGESTED
    assert batch.priority == 3
    assert batch.source_id == "scanner_local"
    assert len(batch.documents) == 2
    assert any(tmp_path.joinpath("archive").rglob("page1.pdf"))


def test_packager_quarantines_unreadable(tmp_path: Path) -> None:
    from docuvision.adapters.ingestion.config import IngestionSourceConfig

    settings = Settings(data_root=tmp_path / "data", database_url=None)
    storage = LocalStorageAdapter(settings)
    repo = SqlitePipelineRepository(tmp_path / "dv.db", actor="test")
    packager = BatchPackager(storage, repo, archive_root=tmp_path / "archive")

    drop = tmp_path / "drop"
    drop.mkdir()
    quarantine = tmp_path / "quarantine"
    source = IngestionSourceConfig(
        source_id="bad_src",
        display_name="Bad",
        type="local_directory",
        path=drop,
        batch_priority=1,
        polling_interval_seconds=1,
        file_filters=(".pdf",),
        quarantine_path=quarantine,
    )
    empty = drop / "empty.pdf"
    empty.write_bytes(b"")
    assert packager.ingest_group(source, [empty], group_key="__root__") is None
    assert not empty.exists()
    assert (quarantine / "empty.pdf").exists()

    events = []
    with repo._connect() as connection:
        rows = connection.execute(
            "SELECT action, details FROM pipeline_events WHERE action = ?",
            ("INGEST_QUARANTINE",),
        ).fetchall()
        events = list(rows)
    assert events
    assert "high" in (events[0][1] or "")


def test_watcher_ingests_stable_file(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    quarantine = tmp_path / "quarantine"
    yaml_path = _write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            {
                "source_id": "auto_a",
                "display_name": "Auto A",
                "type": "local_directory",
                "path": str(drop),
                "batch_priority": 2,
                "polling_interval_seconds": 1,
                "file_filters": [".pdf"],
                "quarantine_path": str(quarantine),
                "batch_window_seconds": 1,
            }
        ],
    )
    settings = Settings(
        data_root=tmp_path / "data",
        database_url=None,
        ingestion_sources_path=yaml_path,
    )
    storage = LocalStorageAdapter(settings)
    repo = SqlitePipelineRepository(tmp_path / "dv.db", actor="test")
    sources = load_ingestion_sources(yaml_path)
    watcher = IngestionWatcher(
        settings,
        storage,
        repo,
        sources=sources,
        settle_seconds=0.05,
    )

    thread = threading.Thread(target=watcher.start, daemon=True)
    thread.start()
    try:
        target = drop / "intake.pdf"
        target.write_bytes(b"%PDF-stable-content")
        deadline = time.time() + 12
        batch_id = None
        while time.time() < deadline:
            pending = repo.list_pending_batches()
            if pending:
                batch_id = pending[0].id
                break
            time.sleep(0.2)
        assert batch_id is not None
        assert not target.exists()
        assert "intake.pdf" in storage.list_incoming()
        batch = repo.get_batch(batch_id)
        assert batch is not None
        assert batch.source_id == "auto_a"
        assert batch.documents[0].file_name == "intake.pdf"
        doc = repo.get_job(batch.documents[0].document_id)
        assert doc is not None
        assert doc.status == PipelineStatus.QUEUED
    finally:
        watcher.stop()
        thread.join(timeout=5)


def test_ingestion_status_endpoint(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    yaml_path = _write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            {
                "source_id": "status_src",
                "display_name": "Status Src",
                "type": "network_share_poll",
                "path": str(drop),
                "batch_priority": 2,
                "polling_interval_seconds": 5,
                "file_filters": [".pdf"],
                "quarantine_path": str(tmp_path / "q"),
            }
        ],
    )
    settings = Settings(
        data_root=tmp_path / "data",
        database_url=None,
        ingestion_sources_path=yaml_path,
        ingestion_status_stale_seconds=120,
    )
    settings.ensure_directories()
    store = IngestionStatusStore(settings.resolved_ingestion_status_path())
    store.mark_started(
        [
            SourceRuntimeStatus(
                source_id="status_src",
                display_name="Status Src",
                type="network_share_poll",
                path=str(drop),
                online=True,
                health="healthy",
                queue_depth=3,
                listener="poll",
            )
        ]
    )

    storage = LocalStorageAdapter(settings)
    repository = SqlitePipelineRepository(tmp_path / "api.db", actor="test-api")
    app = create_app()
    client = TestClient(app)
    client.__enter__()
    app.state.settings = settings
    app.state.storage = storage
    app.state.repository = repository
    app.state.repository_kind = "sqlite"

    response = client.get("/api/ingestion/status", headers=LOCAL_API_HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is True
    assert payload["online"] is True
    assert payload["total_queue_depth"] == 3
    assert payload["sources"][0]["source_id"] == "status_src"
    assert payload["sources"][0]["queue_depth"] == 3
    assert payload["stale"] is False
