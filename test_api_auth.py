"""P0 local API auth gate — handshake middleware + key sources."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from conftest import LOCAL_API_HEADERS
from docuvision.api.auth import (
    reset_api_key_cache,
    resolve_and_persist_api_key,
    write_token_lock,
)
from docuvision.api.main import create_app
from docuvision.config.settings import get_settings


def test_api_rejects_missing_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "gate-test-key")
    reset_api_key_cache()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        bare = client.get("/api/health")
        assert bare.status_code == 401
        assert bare.json()["detail"]
        ok = client.get("/api/health", headers={"X-API-Key": "gate-test-key"})
        assert ok.status_code == 200
        bearer = client.get(
            "/api/health",
            headers={"Authorization": "Bearer gate-test-key"},
        )
        assert bearer.status_code == 200


def test_api_accepts_query_param_for_file_stream(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "query-test-key")
    reset_api_key_cache()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        denied = client.get("/api/health")
        assert denied.status_code == 401
        allowed = client.get("/api/health?api_key=query-test-key")
        assert allowed.status_code == 200


def test_session_token_written_to_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    monkeypatch.delenv("DOCUVISION_LOCAL_API_KEY", raising=False)
    reset_api_key_cache()
    get_settings.cache_clear()
    lock = tmp_path / "token.lock"
    key = resolve_and_persist_api_key(force_refresh=True)
    # Force write to temp path to avoid touching the real AppData path.
    write_token_lock(key, path=lock)
    assert lock.is_file()
    assert lock.read_text(encoding="utf-8") == key
    assert len(key) >= 32


def test_default_conftest_headers_work() -> None:
    reset_api_key_cache()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/health", headers=LOCAL_API_HEADERS)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
