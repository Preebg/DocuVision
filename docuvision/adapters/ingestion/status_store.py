"""Durable heartbeat / queue-depth snapshot for the ingestion status API.

The watcher daemon and the FastAPI process are typically separate on-prem
services sharing ``DOCUVISION_DATA_ROOT``. Status is written atomically to a
JSON file under that root so the API stays read-only and offline-compatible.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATUS_RELATIVE = Path("ingestion") / "status.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceRuntimeStatus:
    source_id: str
    display_name: str
    type: str
    path: str
    online: bool = False
    health: str = "unknown"  # healthy | degraded | offline | unknown
    queue_depth: int = 0
    last_poll_at: str | None = None
    last_ingest_at: str | None = None
    last_error: str | None = None
    batches_formed: int = 0
    files_ingested: int = 0
    files_quarantined: int = 0
    listener: str = "none"  # watchdog | poll


@dataclass
class IngestionDaemonStatus:
    daemon: str = "ingestion-watcher"
    running: bool = False
    started_at: str | None = None
    updated_at: str | None = None
    sources: list[SourceRuntimeStatus] = field(default_factory=list)


class IngestionStatusStore:
    """Thread-safe in-memory status mirror with atomic disk persistence."""

    def __init__(self, status_path: Path) -> None:
        self._path = status_path
        self._lock = threading.RLock()
        self._status = IngestionDaemonStatus()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def mark_started(self, sources: list[SourceRuntimeStatus]) -> None:
        with self._lock:
            now = utc_now_iso()
            self._status = IngestionDaemonStatus(
                running=True,
                started_at=now,
                updated_at=now,
                sources=list(sources),
            )
            self._persist_unlocked()

    def upsert_source(self, status: SourceRuntimeStatus) -> None:
        with self._lock:
            replaced = False
            updated: list[SourceRuntimeStatus] = []
            for item in self._status.sources:
                if item.source_id == status.source_id:
                    updated.append(status)
                    replaced = True
                else:
                    updated.append(item)
            if not replaced:
                updated.append(status)
            self._status.sources = updated
            self._status.updated_at = utc_now_iso()
            self._persist_unlocked()

    def touch(self) -> None:
        with self._lock:
            self._status.updated_at = utc_now_iso()
            self._status.running = True
            self._persist_unlocked()

    def mark_stopped(self) -> None:
        with self._lock:
            self._status.running = False
            self._status.updated_at = utc_now_iso()
            self._persist_unlocked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "daemon": self._status.daemon,
                "running": self._status.running,
                "started_at": self._status.started_at,
                "updated_at": self._status.updated_at,
                "sources": [asdict(item) for item in self._status.sources],
            }

    def _persist_unlocked(self) -> None:
        payload = {
            "daemon": self._status.daemon,
            "running": self._status.running,
            "started_at": self._status.started_at,
            "updated_at": self._status.updated_at,
            "sources": [asdict(item) for item in self._status.sources],
        }
        try:
            self._atomic_write_json(self._path, payload)
        except OSError:
            logger.exception("ingestion.status_persist_failed path=%s", self._path)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".ingestion_status_",
            suffix=".json",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            IngestionStatusStore._replace_file(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _replace_file(src: str, dest: Path) -> None:
        """Atomically replace ``dest`` with ``src``.

        On Windows, ``os.replace`` often raises ``PermissionError`` (WinError 5)
        when another process briefly holds ``status.json`` (API reads, Defender).
        Retry, then fall back to an in-place overwrite so heartbeats keep flowing.
        """
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                os.replace(src, dest)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.025 * (attempt + 1))
            except OSError as exc:
                # WinError 5 / 32 (sharing violation) show up as OSError subclasses.
                if getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                last_error = exc
                time.sleep(0.025 * (attempt + 1))

        try:
            with open(src, "rb") as reader, open(dest, "wb") as writer:
                writer.write(reader.read())
                writer.flush()
                os.fsync(writer.fileno())
            os.unlink(src)
            logger.warning(
                "ingestion.status_replace_fallback path=%s error=%s",
                dest,
                last_error,
            )
        except OSError:
            if last_error is not None:
                raise last_error
            raise

    @classmethod
    def read_file(cls, status_path: Path) -> dict[str, Any] | None:
        """Load the on-disk snapshot for API consumers (None if missing/corrupt)."""
        if not status_path.is_file():
            return None
        try:
            with status_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, json.JSONDecodeError):
            logger.warning("ingestion.status_read_failed path=%s", status_path)
            return None


def default_status_path(data_root: Path) -> Path:
    return Path(data_root) / STATUS_RELATIVE
