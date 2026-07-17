"""P0 local API auth gate — handshake middleware + key sources."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from conftest import LOCAL_API_HEADERS, operator_auth_headers
from docuvision.api.auth import (
    reset_api_key_cache,
    resolve_and_persist_api_key,
    write_token_lock,
)
from docuvision.api.main import create_app
from docuvision.api.operator_auth import reset_operator_auth_caches
from docuvision.api.operator_auth import provision_operator
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
        assert bearer.status_code == 401


def test_api_rejects_query_string_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "query-test-key")
    reset_api_key_cache()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        denied = client.get("/api/health")
        assert denied.status_code == 401
        rejected = client.get("/api/health?api_key=query-test-key")
        assert rejected.status_code == 401


def test_sensitive_route_requires_valid_operator_jwt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DOCUVISION_DATA_ROOT", str(tmp_path))
    reset_api_key_cache()
    get_settings.cache_clear()
    reset_operator_auth_caches()
    app = create_app()
    with TestClient(app) as client:
        missing = client.patch(
            "/api/documents/missing",
            json={"action": "approve"},
            headers=LOCAL_API_HEADERS,
        )
        assert missing.status_code == 401

        invalid = client.patch(
            "/api/documents/missing",
            json={"action": "approve"},
            headers={
                **LOCAL_API_HEADERS,
                "Authorization": "Bearer invalid",
                "X-Operator-Identity": "spoofed",
            },
        )
        assert invalid.status_code == 401

        auth_headers, _operator_id = operator_auth_headers(client)
        valid = client.patch(
            "/api/documents/missing",
            json={"action": "approve"},
            headers=auth_headers,
        )
        assert valid.status_code == 404


def test_refresh_tokens_rotate_and_replay_revokes_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DOCUVISION_DATA_ROOT", str(tmp_path))
    reset_api_key_cache()
    get_settings.cache_clear()
    reset_operator_auth_caches()
    provision_operator("rotation_test", "correct-horse-battery-staple")
    app = create_app()
    with TestClient(app) as client:
        denied = client.post(
            "/api/auth/login",
            json={"username": "rotation_test", "password": "wrong-password-value"},
            headers=LOCAL_API_HEADERS,
        )
        assert denied.status_code == 401

        login = client.post(
            "/api/auth/login",
            json={
                "username": "rotation_test",
                "password": "correct-horse-battery-staple",
            },
            headers=LOCAL_API_HEADERS,
        )
        assert login.status_code == 200
        original = login.json()
        rotated = client.post(
            "/api/auth/refresh",
            json={
                "session_id": original["session_id"],
                "refresh_token": original["refresh_token"],
            },
            headers=LOCAL_API_HEADERS,
        )
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != original["refresh_token"]

        replay = client.post(
            "/api/auth/refresh",
            json={
                "session_id": original["session_id"],
                "refresh_token": original["refresh_token"],
            },
            headers=LOCAL_API_HEADERS,
        )
        assert replay.status_code == 401

        revoked_access = client.patch(
            "/api/documents/missing",
            json={"action": "approve"},
            headers={
                **LOCAL_API_HEADERS,
                "Authorization": f"Bearer {rotated.json()['access_token']}",
            },
        )
        assert revoked_access.status_code == 401


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
