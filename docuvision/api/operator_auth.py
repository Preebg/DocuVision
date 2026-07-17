"""On-prem operator identity, session issuance, and JWT verification."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Iterator

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from docuvision.config.settings import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["operator-auth"])
_bearer = HTTPBearer(auto_error=False)
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_UNAUTHORIZED = "Invalid operator credentials or session."
_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
_dummy_hash = _password_hasher.hash("not-a-real-operator-password")


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    id: str
    username: str
    display_name: str | None
    role: str
    session_id: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=1024)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=36, max_length=36)
    refresh_token: str = Field(min_length=43, max_length=128)


class OperatorOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    role: str


class SessionOut(BaseModel):
    access_token: str
    access_expires_at: int
    refresh_token: str
    session_id: str
    operator: OperatorOut


def _utc_timestamp() -> int:
    return int(time.time())


def _normalize_username(value: str) -> str:
    username = value.strip().casefold()
    if not _USERNAME.fullmatch(username):
        raise ValueError(
            "Username must be 3-64 lowercase letters, digits, dot, dash, or underscore."
        )
    return username


def _database_path(settings: Settings) -> Path:
    return settings.resolved_operator_auth_db_path()


@contextmanager
def _connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    path = _database_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS operators (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'operator',
            password_hash TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operator_sessions (
            id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
            refresh_token_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            created_at INTEGER NOT NULL,
            last_rotated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operator_sessions_operator
            ON operator_sessions(operator_id);
        """
    )
    if is_new:
        _harden_private_file(path)
    try:
        yield connection
    finally:
        connection.close()


def _harden_private_file(path: Path) -> None:
    if os.name == "nt":
        import subprocess

        user = os.environ.get("USERNAME")
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
    else:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@lru_cache(maxsize=1)
def _signing_key() -> Ed25519PrivateKey:
    settings = get_settings()
    path = settings.resolved_operator_signing_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError("Operator signing key is not Ed25519.")
        return key

    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise RuntimeError("Operator signing key is not Ed25519.")
        return loaded
    _harden_private_file(path)
    return key


def _refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _operator_out(row: sqlite3.Row) -> OperatorOut:
    return OperatorOut(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=row["display_name"],
        role=str(row["role"]),
    )


def _issue_access_token(
    operator: OperatorOut,
    session_id: str,
    settings: Settings,
) -> tuple[str, int]:
    now = _utc_timestamp()
    expires_at = now + settings.operator_access_token_seconds
    token = jwt.encode(
        {
            "iss": settings.operator_jwt_issuer,
            "aud": settings.operator_jwt_audience,
            "sub": operator.id,
            "username": operator.username,
            "display_name": operator.display_name,
            "role": operator.role,
            "session_id": session_id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": expires_at,
        },
        _signing_key(),
        algorithm="EdDSA",
    )
    return token, expires_at


def _session_response(
    operator: OperatorOut,
    session_id: str,
    refresh_token: str,
    settings: Settings,
) -> SessionOut:
    access_token, access_expires_at = _issue_access_token(
        operator, session_id, settings
    )
    return SessionOut(
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=refresh_token,
        session_id=session_id,
        operator=operator,
    )


def provision_operator(
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    role: str = "operator",
    settings: Settings | None = None,
) -> OperatorOut:
    """Create an operator account; intended for the local administration CLI."""
    settings = settings or get_settings()
    normalized = _normalize_username(username)
    if len(password) < settings.operator_password_min_length:
        raise ValueError(
            f"Password must be at least {settings.operator_password_min_length} characters."
        )
    if role not in {"operator", "supervisor", "admin"}:
        raise ValueError("Role must be operator, supervisor, or admin.")
    now = datetime.now(timezone.utc).isoformat()
    operator_id = str(uuid.uuid4())
    password_hash = _password_hasher.hash(password)
    with _connect(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO operators (
                    id, username, display_name, role, password_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operator_id,
                    normalized,
                    display_name.strip() if display_name else None,
                    role,
                    password_hash,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Operator username already exists.") from exc
    return OperatorOut(
        id=operator_id,
        username=normalized,
        display_name=display_name.strip() if display_name else None,
        role=role,
    )


def set_operator_disabled(
    username: str,
    *,
    disabled: bool,
    settings: Settings | None = None,
) -> None:
    """Disable/enable a local account and revoke all sessions when disabled."""
    settings = settings or get_settings()
    normalized = _normalize_username(username)
    now = _utc_timestamp()
    with _connect(settings) as connection:
        row = connection.execute(
            "SELECT id FROM operators WHERE username = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise ValueError("Operator does not exist.")
        connection.execute(
            "UPDATE operators SET disabled = ?, updated_at = ? WHERE id = ?",
            (
                int(disabled),
                datetime.now(timezone.utc).isoformat(),
                row["id"],
            ),
        )
        if disabled:
            connection.execute(
                """
                UPDATE operator_sessions SET revoked_at = ?
                WHERE operator_id = ? AND revoked_at IS NULL
                """,
                (now, row["id"]),
            )


def change_operator_password(
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Replace an Argon2id credential and revoke every existing session."""
    settings = settings or get_settings()
    normalized = _normalize_username(username)
    if len(password) < settings.operator_password_min_length:
        raise ValueError(
            f"Password must be at least {settings.operator_password_min_length} characters."
        )
    now = _utc_timestamp()
    with _connect(settings) as connection:
        row = connection.execute(
            "SELECT id FROM operators WHERE username = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise ValueError("Operator does not exist.")
        connection.execute(
            """
            UPDATE operators
            SET password_hash = ?, failed_attempts = 0, locked_until = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                _password_hasher.hash(password),
                datetime.now(timezone.utc).isoformat(),
                row["id"],
            ),
        )
        connection.execute(
            """
            UPDATE operator_sessions SET revoked_at = ?
            WHERE operator_id = ? AND revoked_at IS NULL
            """,
            (now, row["id"]),
        )


@router.post("/login", response_model=SessionOut)
def login(body: LoginRequest) -> SessionOut:
    settings = get_settings()
    try:
        username = _normalize_username(body.username)
    except ValueError:
        username = ""
    now = _utc_timestamp()
    with _connect(settings) as connection:
        row = connection.execute(
            "SELECT * FROM operators WHERE username = ?",
            (username,),
        ).fetchone()
        stored_hash = str(row["password_hash"]) if row is not None else _dummy_hash
        password_valid = False
        try:
            password_valid = _password_hasher.verify(stored_hash, body.password)
        except (VerifyMismatchError, InvalidHashError):
            pass

        locked = row is not None and int(row["locked_until"] or 0) > now
        disabled = row is not None and bool(row["disabled"])
        if row is None or not password_valid or locked or disabled:
            if row is not None and not disabled:
                attempts = int(row["failed_attempts"]) + 1
                locked_until = (
                    now + settings.operator_lockout_seconds
                    if attempts >= settings.operator_max_failed_attempts
                    else None
                )
                connection.execute(
                    """
                    UPDATE operators
                    SET failed_attempts = ?, locked_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        attempts,
                        locked_until,
                        datetime.now(timezone.utc).isoformat(),
                        row["id"],
                    ),
                )
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)

        if _password_hasher.check_needs_rehash(stored_hash):
            connection.execute(
                "UPDATE operators SET password_hash = ? WHERE id = ?",
                (_password_hasher.hash(body.password), row["id"]),
            )
        connection.execute(
            """
            UPDATE operators
            SET failed_attempts = 0, locked_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        operator = _operator_out(row)
        session_id = str(uuid.uuid4())
        refresh_token = secrets.token_urlsafe(48)
        connection.execute(
            """
            INSERT INTO operator_sessions (
                id, operator_id, refresh_token_hash, expires_at,
                created_at, last_rotated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                operator.id,
                _refresh_hash(refresh_token),
                now + settings.operator_refresh_token_seconds,
                now,
                now,
            ),
        )
    return _session_response(operator, session_id, refresh_token, settings)


@router.post("/refresh", response_model=SessionOut)
def refresh(body: RefreshRequest) -> SessionOut:
    settings = get_settings()
    now = _utc_timestamp()
    replacement = secrets.token_urlsafe(48)
    with _connect(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT s.*, o.username, o.display_name, o.role, o.disabled
            FROM operator_sessions s
            JOIN operators o ON o.id = s.operator_id
            WHERE s.id = ?
            """,
            (body.session_id,),
        ).fetchone()
        valid = (
            row is not None
            and row["revoked_at"] is None
            and int(row["expires_at"]) > now
            and not bool(row["disabled"])
            and secrets.compare_digest(
                str(row["refresh_token_hash"]),
                _refresh_hash(body.refresh_token),
            )
        )
        if not valid:
            if row is not None:
                connection.execute(
                    "UPDATE operator_sessions SET revoked_at = ? WHERE id = ?",
                    (now, body.session_id),
                )
            connection.commit()
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
        connection.execute(
            """
            UPDATE operator_sessions
            SET refresh_token_hash = ?, last_rotated_at = ?
            WHERE id = ?
            """,
            (_refresh_hash(replacement), now, body.session_id),
        )
        connection.commit()
        operator = OperatorOut(
            id=str(row["operator_id"]),
            username=str(row["username"]),
            display_name=row["display_name"],
            role=str(row["role"]),
        )
    return _session_response(operator, body.session_id, replacement, settings)


@router.post("/logout", status_code=204)
def logout(body: RefreshRequest) -> None:
    settings = get_settings()
    with _connect(settings) as connection:
        row = connection.execute(
            "SELECT refresh_token_hash FROM operator_sessions WHERE id = ?",
            (body.session_id,),
        ).fetchone()
        if row is not None and secrets.compare_digest(
            str(row["refresh_token_hash"]),
            _refresh_hash(body.refresh_token),
        ):
            connection.execute(
                "UPDATE operator_sessions SET revoked_at = ? WHERE id = ?",
                (_utc_timestamp(), body.session_id),
            )


def _decode_access_token(token: str, settings: Settings) -> dict[str, object]:
    public_key = _signing_key().public_key()
    return jwt.decode(
        token,
        public_key,
        algorithms=["EdDSA"],
        audience=settings.operator_jwt_audience,
        issuer=settings.operator_jwt_issuer,
        leeway=settings.operator_jwt_leeway_seconds,
        options={
            "require": [
                "exp",
                "iat",
                "nbf",
                "sub",
                "iss",
                "aud",
                "jti",
                "session_id",
            ]
        },
    )


def require_operator(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ] = None,
) -> OperatorPrincipal:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    try:
        claims = _decode_access_token(credentials.credentials.strip(), settings)
        subject = str(claims["sub"])
        session_id = str(claims["session_id"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=401,
            detail=_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Sensitive actions check live session/account state for immediate revocation.
    with _connect(settings) as connection:
        row = connection.execute(
            """
            SELECT o.id, o.username, o.display_name, o.role
            FROM operator_sessions s
            JOIN operators o ON o.id = s.operator_id
            WHERE s.id = ? AND s.operator_id = ?
              AND s.revoked_at IS NULL AND s.expires_at > ?
              AND o.disabled = 0
            """,
            (session_id, subject, _utc_timestamp()),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)

    principal = OperatorPrincipal(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=row["display_name"],
        role=str(row["role"]),
        session_id=session_id,
    )
    request.state.operator = principal
    return principal


def reset_operator_auth_caches() -> None:
    """Test helper for isolated signing-key/settings environments."""
    _signing_key.cache_clear()
