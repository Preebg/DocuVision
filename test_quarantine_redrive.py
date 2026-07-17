"""Quarantine persistence, storage isolation, redrive, and API authorization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import LOCAL_API_HEADERS
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.api.auth import reset_api_key_cache
from docuvision.api.main import create_app
from docuvision.api.operator_auth import (
    provision_operator,
    reset_operator_auth_caches,
)
from docuvision.config.settings import Settings, get_settings
from docuvision.core.models import PipelineStatus, QuarantineReason
from docuvision.core.quarantine import QuarantineService


def _isolated(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    storage = LocalStorageAdapter(settings)
    repository = SqlitePipelineRepository(tmp_path / "pipeline.db", actor="test")
    return settings, storage, repository


def _quarantine_one(
    storage: LocalStorageAdapter,
    repository: SqlitePipelineRepository,
    *,
    name: str = "bad.pdf",
):
    stored = storage.write_incoming(name, b"%PDF-1.4 broken")
    job = repository.create_job(stored)
    item = repository.begin_quarantine(
        job.id,
        reason=QuarantineReason.PARSING_ERROR,
        error_stack="Traceback\nValueError: malformed xref",
        failed_stage="LAYOUT",
    )
    locator = storage.quarantine_payload(job.id, stored, None)
    repository.set_quarantine_locator(item.id, locator)
    return job, repository.get_active_quarantine(job.id)


def test_migration_and_quarantine_round_trip(tmp_path: Path) -> None:
    _settings, storage, repository = _isolated(tmp_path)
    job, item = _quarantine_one(storage, repository)

    assert item is not None
    assert item.reason == QuarantineReason.PARSING_ERROR
    assert item.error_stack and "malformed xref" in item.error_stack
    assert item.payload_locator == f"{job.id}/bad.pdf"
    assert repository.get_job(job.id).status == PipelineStatus.QUARANTINED
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM quarantine_attempts"
        ).fetchone()[0] == 1


def test_redrive_restores_payload_and_clears_current_error(tmp_path: Path) -> None:
    settings, storage, repository = _isolated(tmp_path)
    job, _item = _quarantine_one(storage, repository)

    result = QuarantineService(repository, storage).redrive(
        [job.id],
        actor="admin-id",
    )[0]

    assert result.outcome == "queued"
    queued = repository.get_job(job.id)
    assert queued is not None
    assert queued.status == PipelineStatus.QUEUED
    assert queued.retry_count == 1
    assert queued.error_message is None
    assert queued.layout_markdown is None
    assert (settings.resolved_incoming_dir() / "bad.pdf").is_file()
    assert repository.get_active_quarantine(job.id) is None


def test_legacy_failed_document_can_be_redriven(tmp_path: Path) -> None:
    settings, storage, repository = _isolated(tmp_path)
    storage.write_incoming("legacy.pdf", b"%PDF-1.4")
    job = repository.create_job("legacy.pdf")
    repository.mark_failed(job.id, "old worker failure")
    storage.mark_failed("legacy.pdf", None)

    result = QuarantineService(repository, storage).redrive(
        [job.id],
        actor="admin-id",
    )[0]

    assert result.outcome == "queued"
    assert (settings.resolved_incoming_dir() / "legacy.pdf").is_file()


def test_storage_rejects_quarantine_locator_escape(tmp_path: Path) -> None:
    _settings, storage, _repository = _isolated(tmp_path)
    try:
        storage.restore_for_redrive("bad.pdf", "../../outside.pdf")
    except ValueError as exc:
        assert "outside configured root" in str(exc)
    else:
        raise AssertionError("Traversal locator was accepted.")


def test_storage_refuses_redrive_destination_collision(tmp_path: Path) -> None:
    _settings, storage, repository = _isolated(tmp_path)
    _job, item = _quarantine_one(storage, repository)
    assert item is not None and item.payload_locator
    storage.write_incoming("bad.pdf", b"different payload")
    with pytest.raises(FileExistsError):
        storage.restore_for_redrive("bad.pdf", item.payload_locator)


def test_concurrent_redrive_reservation_has_single_winner(tmp_path: Path) -> None:
    _settings, storage, repository = _isolated(tmp_path)
    job, _item = _quarantine_one(storage, repository)

    def reserve(token: str):
        return repository.reserve_redrive(job.id, actor="admin", token=token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["token-a", "token-b"]))
    assert sum(item is not None for item in results) == 1


def _login_headers(client: TestClient, username: str, role: str) -> dict[str, str]:
    provision_operator(
        username,
        "correct-horse-battery-staple",
        role=role,
    )
    response = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": "correct-horse-battery-staple",
        },
        headers=LOCAL_API_HEADERS,
    )
    assert response.status_code == 200, response.text
    return {
        **LOCAL_API_HEADERS,
        "Authorization": f"Bearer {response.json()['access_token']}",
    }


def test_quarantine_api_lists_and_bulk_redrives_with_admin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DOCUVISION_DATA_ROOT", str(tmp_path / "runtime"))
    reset_api_key_cache()
    get_settings.cache_clear()
    reset_operator_auth_caches()
    settings, storage, repository = _isolated(tmp_path)
    job, _item = _quarantine_one(storage, repository)
    batch = repository.create_batch(
        name="redrive-batch",
        documents=[(job.id, 1)],
    )

    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.storage = storage
        app.state.repository = repository
        headers = _login_headers(client, "quarantine_admin", "admin")

        listed = client.get("/api/v1/quarantine/items", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["document_id"] == job.id
        assert "payload_locator" not in listed.json()["items"][0]

        redriven = client.post(
            "/api/v1/quarantine/redrive",
            json={"batch_id": batch.id},
            headers=headers,
        )
        assert redriven.status_code == 200, redriven.text
        assert redriven.json()["results"][0]["outcome"] == "queued"


def test_quarantine_api_rejects_operator_role(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DOCUVISION_DATA_ROOT", str(tmp_path / "runtime"))
    reset_api_key_cache()
    get_settings.cache_clear()
    reset_operator_auth_caches()
    settings, storage, repository = _isolated(tmp_path)
    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.storage = storage
        app.state.repository = repository
        headers = _login_headers(client, "quarantine_operator", "operator")
        denied = client.get("/api/v1/quarantine/items", headers=headers)
        assert denied.status_code == 403
