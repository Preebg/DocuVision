"""Local loopback API key gate — session token + optional LOCAL_API_KEY fallback.

The FastAPI process either loads a static key from the environment (``.env`` /
``LOCAL_API_KEY``) or mints a high-entropy single-session token. The active key
is written to a per-user lockfile so the Tauri desktop shell can read it with
OS-level user permissions and present it on every ``/api/...`` request.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

from typing import Annotated

from fastapi import HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
API_KEY_QUERY_PARAM = "api_key"
_UNAUTHORIZED_DETAIL = (
    "Unauthorized. Provide a valid X-API-Key header or Bearer token."
)
_UNAUTHORIZED = JSONResponse(
    status_code=401,
    content={"detail": _UNAUTHORIZED_DETAIL},
)

# Declared for OpenAPI + Depends(); middleware is the global `/api` enforcer.
api_key_header_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

_active_api_key: str | None = None


def token_lock_path() -> Path:
    """Per-user lockfile path for the session API token hand-off."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "DocuVision" / "token.lock"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "DocuVision"
            / "token.lock"
        )
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state) if xdg_state else (Path.home() / ".local" / "state")
    return root / "DocuVision" / "token.lock"


def _configured_api_key() -> str | None:
    """Static fallback from env / settings (testing and fixed deployments)."""
    for name in ("LOCAL_API_KEY", "DOCUVISION_LOCAL_API_KEY"):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    try:
        from docuvision.config.settings import get_settings

        configured = get_settings().local_api_key
        if configured is not None and str(configured).strip():
            return str(configured).strip()
    except Exception:  # pragma: no cover - settings bootstrap edge
        pass
    return None


def _harden_lockfile_permissions(path: Path) -> None:
    """Restrict the lockfile to the current user where the OS allows it."""
    try:
        if sys.platform == "win32":
            # AppData\\Local is already user-scoped; tighten DACL via icacls when available.
            import subprocess

            user = os.environ.get("USERNAME") or os.environ.get("USER")
            if user:
                subprocess.run(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{user}:(R,W)",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            return
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("api.auth.lockfile_chmod_failed path=%s err=%s", path, exc)


def write_token_lock(api_key: str, path: Path | None = None) -> Path:
    """Persist the active key for the Tauri process to read at startup."""
    lock_path = path or token_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create+write; replace atomically where possible.
    tmp_path = lock_path.with_suffix(".lock.tmp")
    tmp_path.write_text(api_key, encoding="utf-8")
    tmp_path.replace(lock_path)
    _harden_lockfile_permissions(lock_path)
    return lock_path


def resolve_and_persist_api_key(*, force_refresh: bool = False) -> str:
    """Return the process API key, generating + writing a session token if needed."""
    global _active_api_key
    if _active_api_key is not None and not force_refresh:
        return _active_api_key

    configured = _configured_api_key()
    if configured:
        api_key = configured
        source = "env"
    else:
        api_key = secrets.token_urlsafe(32)
        source = "session"

    lock_path = write_token_lock(api_key)
    _active_api_key = api_key
    logger.info(
        "api.auth.ready source=%s lockfile=%s key_prefix=%s…",
        source,
        lock_path,
        api_key[:4],
    )
    return api_key


def get_active_api_key() -> str:
    """Return the key for this process (resolving on first use)."""
    return resolve_and_persist_api_key()


def reset_api_key_cache() -> None:
    """Test helper — clear the in-process key cache."""
    global _active_api_key
    _active_api_key = None


def extract_presented_api_key(request: Request) -> str | None:
    """Pull the key from ``X-API-Key``, ``Authorization: Bearer``, or ``?api_key=``."""
    header_key = request.headers.get(API_KEY_HEADER)
    if header_key is not None and header_key.strip():
        return header_key.strip()

    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()

    query_key = request.query_params.get(API_KEY_QUERY_PARAM)
    if query_key is not None and query_key.strip():
        return query_key.strip()

    return None


def api_key_matches(presented: str | None, expected: str) -> bool:
    if presented is None:
        return False
    return secrets.compare_digest(presented, expected)


async def require_local_api_key(
    api_key: Annotated[str | None, Security(api_key_header_scheme)] = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ] = None,
    api_key_query: Annotated[str | None, Query(alias=API_KEY_QUERY_PARAM)] = None,
) -> str:
    """FastAPI dependency — accept ``X-API-Key``, Bearer, or ``?api_key=``."""
    expected = get_active_api_key()
    presented: str | None = None
    if api_key is not None and api_key.strip():
        presented = api_key.strip()
    elif credentials is not None and credentials.scheme.lower() == "bearer":
        token = (credentials.credentials or "").strip()
        presented = token or None
    elif api_key_query is not None and api_key_query.strip():
        presented = api_key_query.strip()

    if not api_key_matches(presented, expected):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)
    return expected


class LocalApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated traffic to ``/api/...`` with a clean 401 JSON body."""

    def __init__(self, app: ASGIApp, api_key: str | None = None) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        # CORS preflight must pass without credentials.
        if request.method == "OPTIONS":
            return await call_next(request)

        expected = self._api_key or get_active_api_key()
        presented = extract_presented_api_key(request)
        if not api_key_matches(presented, expected):
            return _UNAUTHORIZED
        return await call_next(request)


def install_api_auth(app: ASGIApp) -> str:
    """Resolve the session/static key, write the lockfile, attach middleware.

    Returns the active API key (also stored on ``app.state`` when available).
    """
    api_key = resolve_and_persist_api_key()
    # FastAPI apps expose `.add_middleware`; plain ASGI may not.
    add_middleware = getattr(app, "add_middleware", None)
    if callable(add_middleware):
        add_middleware(LocalApiKeyMiddleware, api_key=api_key)
    state = getattr(app, "state", None)
    if state is not None:
        state.local_api_key = api_key
        state.api_token_lock_path = str(token_lock_path())
    return api_key
