"""Production-grade directory monitor for Pillar 1 local / SMB / NFS ingestion.

Dual listener strategy
----------------------
* ``local_directory`` — prefer an OS watchdog Observer thread; fall back to
  a robust poll loop when watchdog is unavailable or the path is not mounted.
* ``network_share_poll`` — always use interval heartbeat polls so transient
  SMB/NFS disconnects degrade to ``offline`` instead of crashing the daemon.

Safety lock
-----------
Files are only packaged after a settle check: size/mtime must be unchanged for
``settle_seconds`` (default 2s), and an exclusive read open must succeed
(Windows share-mode / POSIX advisory).

Batching
--------
Pending stable files are grouped by subfolder (or flat drop) and flushed when
the source's ``batch_window_seconds`` elapses since the newest arrival.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from docuvision.adapters.ingestion.config import (
    IngestionSourceConfig,
    load_ingestion_sources,
)
from docuvision.adapters.ingestion.packager import BatchPackager, group_key_for_path
from docuvision.adapters.ingestion.status_store import (
    IngestionStatusStore,
    SourceRuntimeStatus,
    default_status_path,
    utc_now_iso,
)
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.config.settings import Settings, get_settings
from docuvision.core.contracts import PipelineRepository

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 2.0

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[assignment]
    _WATCHDOG_AVAILABLE = False


@dataclass
class _PendingFile:
    path: Path
    first_seen: float
    last_seen: float
    size: int
    mtime: float


@dataclass
class _SourceRuntime:
    config: IngestionSourceConfig
    status: SourceRuntimeStatus
    pending: dict[str, _PendingFile] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)
    listener: str = "poll"
    observer: object | None = None


class _DropEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Thin watchdog bridge — records paths; packager runs on the poll tick."""

    def __init__(self, runtime: _SourceRuntime, on_path: Callable[[Path], None]) -> None:
        super().__init__()
        self._runtime = runtime
        self._on_path = on_path

    def on_created(self, event: object) -> None:
        self._handle(event)

    def on_modified(self, event: object) -> None:
        self._handle(event)

    def on_moved(self, event: object) -> None:
        dest = getattr(event, "dest_path", None)
        if dest:
            self._on_path(Path(dest))

    def _handle(self, event: object) -> None:
        if getattr(event, "is_directory", False):
            return
        src = getattr(event, "src_path", None)
        if src:
            self._on_path(Path(src))


class IngestionWatcher:
    """Multi-source daemon: watch / poll → settle → batch → archive/quarantine."""

    def __init__(
        self,
        settings: Settings,
        storage: LocalStorageAdapter,
        repository: PipelineRepository,
        *,
        sources: list[IngestionSourceConfig] | None = None,
        status_store: IngestionStatusStore | None = None,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._repository = repository
        self._settle_seconds = settle_seconds
        sources_path = settings.resolved_ingestion_sources_path()
        self._sources = (
            sources
            if sources is not None
            else load_ingestion_sources(
                sources_path,
                data_root=settings.data_root,
            )
        )
        archive_root = settings.resolved_ingestion_archive_dir()
        self._packager = BatchPackager(storage, repository, archive_root=archive_root)
        self._status = status_store or IngestionStatusStore(
            default_status_path(settings.data_root)
        )
        self._runtimes: dict[str, _SourceRuntime] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def status_store(self) -> IngestionStatusStore:
        return self._status

    def start(self) -> None:
        """Spawn per-source listeners and block until ``stop()``."""
        initial: list[SourceRuntimeStatus] = []
        for config in self._sources:
            config.quarantine_path.mkdir(parents=True, exist_ok=True)
            try:
                config.path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "ingestion.source_path_unavailable source=%s path=%s error=%s",
                    config.source_id,
                    config.path,
                    exc,
                )

            status = SourceRuntimeStatus(
                source_id=config.source_id,
                display_name=config.display_name,
                type=config.type,
                path=str(config.path),
            )
            runtime = _SourceRuntime(config=config, status=status)
            self._runtimes[config.source_id] = runtime
            initial.append(status)

            if config.type == "local_directory" and _WATCHDOG_AVAILABLE:
                if self._try_start_watchdog(runtime):
                    runtime.listener = "watchdog"
                    status.listener = "watchdog"
                else:
                    runtime.listener = "poll"
                    status.listener = "poll"
            else:
                runtime.listener = "poll"
                status.listener = "poll"

            thread = threading.Thread(
                target=self._source_loop,
                name=f"ingest-{config.source_id}",
                args=(runtime,),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

        self._status.mark_started(initial)
        logger.info(
            "ingestion.watcher_started sources=%s watchdog=%s",
            len(self._runtimes),
            _WATCHDOG_AVAILABLE,
        )
        try:
            while not self._stop.wait(timeout=1.0):
                self._status.touch()
        finally:
            self._shutdown_listeners()
            self._status.mark_stopped()

    def stop(self) -> None:
        self._stop.set()

    def _try_start_watchdog(self, runtime: _SourceRuntime) -> bool:
        if Observer is None:
            return False
        path = runtime.config.path
        if not path.is_dir():
            return False
        try:
            handler = _DropEventHandler(
                runtime,
                on_path=lambda p: self._note_candidate(runtime, p),
            )
            observer = Observer()
            observer.schedule(handler, str(path), recursive=True)
            observer.daemon = True
            observer.start()
            runtime.observer = observer
            logger.info(
                "ingestion.watchdog_started source=%s path=%s",
                runtime.config.source_id,
                path,
            )
            return True
        except Exception as exc:
            logger.warning(
                "ingestion.watchdog_fallback source=%s error=%s",
                runtime.config.source_id,
                exc,
            )
            return False

    def _shutdown_listeners(self) -> None:
        for runtime in self._runtimes.values():
            observer = runtime.observer
            if observer is not None:
                try:
                    observer.stop()  # type: ignore[attr-defined]
                    observer.join(timeout=5)  # type: ignore[attr-defined]
                except Exception:
                    logger.exception(
                        "ingestion.watchdog_stop_failed source=%s",
                        runtime.config.source_id,
                    )
                runtime.observer = None

    def _source_loop(self, runtime: _SourceRuntime) -> None:
        interval = runtime.config.polling_interval_seconds
        while not self._stop.is_set():
            try:
                self._heartbeat_tick(runtime)
            except Exception:
                logger.exception(
                    "ingestion.source_tick_error source=%s",
                    runtime.config.source_id,
                )
                runtime.status.health = "degraded"
                runtime.status.last_error = "tick_exception"
                self._status.upsert_source(runtime.status)
            self._stop.wait(timeout=interval)

    def _heartbeat_tick(self, runtime: _SourceRuntime) -> None:
        config = runtime.config
        online = self._probe_path(config.path)
        runtime.status.last_poll_at = utc_now_iso()
        runtime.status.online = online
        runtime.status.listener = runtime.listener

        if not online:
            runtime.status.health = "offline"
            runtime.status.queue_depth = len(runtime.pending)
            runtime.status.last_error = "path_unavailable"
            self._status.upsert_source(runtime.status)
            logger.warning(
                "ingestion.source_offline source=%s path=%s",
                config.source_id,
                config.path,
            )
            return

        # Discover candidates (watchdog notes extras; poll always scans).
        for path in self._scan_files(config):
            self._note_candidate(runtime, path)

        self._flush_ready_batches(runtime)

        with runtime.lock:
            runtime.status.queue_depth = len(runtime.pending)
        runtime.status.health = "healthy"
        runtime.status.last_error = None
        self._status.upsert_source(runtime.status)

    def _probe_path(self, path: Path) -> bool:
        """Return True when the directory is reachable (tolerates share blips)."""
        try:
            if not path.exists():
                return False
            if not path.is_dir():
                return False
            # Touch listing — NFS hangs surface as OSError / timeout locally.
            next(path.iterdir(), None)
            return True
        except OSError:
            return False

    def _scan_files(self, config: IngestionSourceConfig) -> list[Path]:
        if not config.path.is_dir():
            return []
        found: list[Path] = []
        try:
            for entry in config.path.rglob("*"):
                if not entry.is_file():
                    continue
                if entry.name.startswith("."):
                    continue
                if not config.matches(entry):
                    continue
                # Skip quarantine / archive trees nested under the watch root.
                try:
                    resolved = entry.resolve()
                    if resolved.is_relative_to(config.quarantine_path.resolve()):
                        continue
                    if config.archive_path and resolved.is_relative_to(
                        config.archive_path.resolve()
                    ):
                        continue
                except (OSError, ValueError):
                    pass
                found.append(entry)
        except OSError as exc:
            logger.warning(
                "ingestion.scan_failed source=%s error=%s",
                config.source_id,
                exc,
            )
        return found

    def _note_candidate(self, runtime: _SourceRuntime, path: Path) -> None:
        config = runtime.config
        try:
            if not path.is_file():
                return
            if path.name.startswith("."):
                return
            if not config.matches(path):
                return
            key = str(path.resolve())
        except OSError:
            return

        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            return

        now = time.monotonic()
        with runtime.lock:
            if key in runtime.seen:
                return
            existing = runtime.pending.get(key)
            if existing is None:
                runtime.pending[key] = _PendingFile(
                    path=path,
                    first_seen=now,
                    last_seen=now,
                    size=size,
                    mtime=mtime,
                )
            else:
                existing.path = path
                # Only reset the batch window when the scanner is still writing.
                if existing.size != size or existing.mtime != mtime:
                    existing.last_seen = now
                    existing.size = size
                    existing.mtime = mtime

    def _flush_ready_batches(self, runtime: _SourceRuntime) -> None:
        config = runtime.config
        now = time.monotonic()
        ready: list[Path] = []

        with runtime.lock:
            pending_items = list(runtime.pending.items())

        for key, pending in pending_items:
            if now - pending.last_seen < config.batch_window_seconds:
                continue
            if not self._is_file_stable(pending.path):
                continue
            ready.append(pending.path)

        if not ready:
            return

        groups: dict[str, list[Path]] = defaultdict(list)
        for path in ready:
            groups[group_key_for_path(config, path)].append(path)

        for group_key, files in groups.items():
            batch_id = self._packager.ingest_group(
                config,
                files,
                group_key=group_key,
            )
            with runtime.lock:
                for path in files:
                    runtime.pending.pop(str(path.resolve()), None)
                    runtime.seen.add(str(path.resolve()))
                if batch_id:
                    runtime.status.batches_formed += 1
                    runtime.status.files_ingested += len(files)
                    runtime.status.last_ingest_at = utc_now_iso()
                else:
                    runtime.status.files_quarantined += len(files)
                    runtime.status.health = "degraded"

    def _is_file_stable(self, path: Path) -> bool:
        """Safety lock — reject files still being written by the scanner."""
        try:
            stat_a = path.stat()
            size_a = stat_a.st_size
            mtime_a = stat_a.st_mtime
        except OSError:
            return False

        if size_a <= 0:
            return False

        time.sleep(self._settle_seconds)

        try:
            stat_b = path.stat()
        except OSError:
            return False

        if stat_b.st_size != size_a or stat_b.st_mtime != mtime_a:
            return False

        # Exclusive / shared-read probe — fails on Windows while writer holds the file.
        try:
            with path.open("rb") as handle:
                handle.read(1)
                handle.seek(0)
        except OSError:
            return False

        return True


def run_ingestion_daemon(settings: Settings | None = None) -> None:
    """Composition helper used by ``run_ingestion.py``."""
    from docuvision.composition import build_backends

    cfg = settings or get_settings()
    cfg.ensure_directories()
    storage, repository = build_backends(cfg)
    watcher = IngestionWatcher(cfg, storage, repository)
    try:
        watcher.start()
    except KeyboardInterrupt:
        watcher.stop()
        logger.info("ingestion.watcher_interrupted")
