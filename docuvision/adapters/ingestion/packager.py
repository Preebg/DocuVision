"""Batch packaging — route stable drop files into Migration 002 contracts."""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from docuvision.adapters.ingestion.config import IngestionSourceConfig
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.core.contracts import PipelineRepository
from docuvision.core.models import PipelineStatus

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")
SYSTEM_AUDIT_ACTOR = "ingestion-watcher"


def sanitize_file_name(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "document"
    return cleaned


def group_key_for_path(source: IngestionSourceConfig, file_path: Path) -> str:
    """Group by immediate subfolder under the source root, else flat drop.

    Files directly in ``source.path`` share key ``__root__``. Files under a
    distinct subfolder (e.g. ``scan_20260714/``) form their own batch key.
    """
    try:
        relative = file_path.resolve().relative_to(source.path.resolve())
    except ValueError:
        return "__root__"
    parts = relative.parts
    if len(parts) >= 2:
        return parts[0]
    return "__root__"


class BatchPackager:
    """Write ingress batches, enroll jobs, archive or quarantine sources."""

    def __init__(
        self,
        storage: LocalStorageAdapter,
        repository: PipelineRepository,
        *,
        archive_root: Path,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._archive_root = archive_root
        self._archive_root.mkdir(parents=True, exist_ok=True)

    def ingest_group(
        self,
        source: IngestionSourceConfig,
        files: list[Path],
        *,
        group_key: str,
    ) -> str | None:
        """Package ``files`` into one batch. Returns batch id, or None on total failure."""
        if not files:
            return None

        readable: list[tuple[Path, str, bytes]] = []
        for path in sorted(files, key=lambda p: p.name.lower()):
            try:
                data = path.read_bytes()
                if not data:
                    raise ValueError("empty file")
                safe_name = sanitize_file_name(path.name)
                readable.append((path, safe_name, data))
            except (OSError, ValueError) as exc:
                logger.error(
                    "ingestion.read_failed source=%s file=%s error=%s",
                    source.source_id,
                    path,
                    exc,
                )
                self.quarantine(
                    source,
                    path,
                    reason=f"unreadable_or_empty: {exc}",
                )

        if not readable:
            return None

        payload = [(safe_name, data) for _, safe_name, data in readable]
        try:
            stored_names = self._storage.write_incoming_batch(payload)
        except Exception as exc:
            logger.exception(
                "ingestion.write_incoming_batch_failed source=%s error=%s",
                source.source_id,
                exc,
            )
            for path, _, _ in readable:
                self.quarantine(source, path, reason=f"write_incoming_failed: {exc}")
            return None

        links: list[tuple[str, int]] = []
        jobs = []
        try:
            for sequence_no, stored_name in enumerate(stored_names, start=1):
                job = self._repository.create_job(stored_name)
                jobs.append(job)
                links.append((job.id, sequence_no))

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder_label = group_key if group_key != "__root__" else "drop"
            batch_name = f"{source.source_id}_{folder_label}_{stamp}"
            batch = self._repository.create_batch(
                name=batch_name,
                priority=source.batch_priority,
                source_id=source.source_id,
                documents=links,
            )
        except Exception as exc:
            logger.exception(
                "ingestion.batch_enroll_failed source=%s error=%s",
                source.source_id,
                exc,
            )
            for path, _, _ in readable:
                self.quarantine(source, path, reason=f"batch_enroll_failed: {exc}")
            for job in jobs:
                try:
                    self._repository.mark_failed(
                        job.id,
                        f"Ingestion enroll failed for source {source.source_id}: {exc}",
                    )
                except Exception:
                    logger.exception("ingestion.mark_failed_cleanup job_id=%s", job.id)
            return None

        archive_dir = self._resolve_archive_dir(source)
        for path, _, _ in readable:
            try:
                self._move_to(path, archive_dir)
            except OSError as exc:
                # Bytes already live under incoming/; remove the drop original
                # so the watcher cannot double-package the same path.
                logger.error(
                    "ingestion.archive_move_failed source=%s file=%s error=%s",
                    source.source_id,
                    path,
                    exc,
                )
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "ingestion.drop_unlink_failed source=%s file=%s",
                        source.source_id,
                        path,
                    )

        for job in jobs:
            try:
                self._repository.emit_audit_event(
                    job_id=job.id,
                    actor=SYSTEM_AUDIT_ACTOR,
                    action="INGEST_SOURCE_BATCH",
                    from_status=None,
                    to_status=PipelineStatus.QUEUED.value,
                    details={
                        "priority": "normal",
                        "source_id": source.source_id,
                        "batch_id": batch.id,
                        "batch_name": batch.name,
                        "group_key": group_key,
                    },
                )
            except Exception:
                logger.exception(
                    "ingestion.audit_failed job_id=%s batch_id=%s",
                    job.id,
                    batch.id,
                )

        logger.info(
            "ingestion.batch_formed source=%s batch_id=%s docs=%s group=%s",
            source.source_id,
            batch.id,
            len(jobs),
            group_key,
        )
        return batch.id

    def quarantine(
        self,
        source: IngestionSourceConfig,
        file_path: Path,
        *,
        reason: str,
    ) -> None:
        """Move a bad file to the source quarantine path and emit a high-priority audit."""
        target_dir = source.quarantine_path
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = self._move_to(file_path, target_dir)
        except OSError as exc:
            logger.error(
                "ingestion.quarantine_move_failed source=%s file=%s error=%s",
                source.source_id,
                file_path,
                exc,
            )
            destination = None

        logger.error(
            "ingestion.quarantine priority=high source=%s file=%s dest=%s reason=%s",
            source.source_id,
            file_path,
            destination,
            reason,
        )

        # Durable audit requires a documents row (FK). Create a sentinel job.
        try:
            marker_name = sanitize_file_name(
                f"quarantine_{source.source_id}_{file_path.name}.log"
            )
            job = self._repository.create_job(marker_name)
            self._repository.mark_failed(
                job.id,
                f"Quarantined from source {source.source_id}: {reason}",
            )
            self._repository.emit_audit_event(
                job_id=job.id,
                actor=SYSTEM_AUDIT_ACTOR,
                action="INGEST_QUARANTINE",
                from_status=None,
                to_status=PipelineStatus.FAILED.value,
                details={
                    "priority": "high",
                    "source_id": source.source_id,
                    "original_path": str(file_path),
                    "quarantine_path": str(destination) if destination else str(target_dir),
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception(
                "ingestion.quarantine_audit_failed source=%s file=%s",
                source.source_id,
                file_path,
            )

    def _resolve_archive_dir(self, source: IngestionSourceConfig) -> Path:
        if source.archive_path is not None:
            root = source.archive_path
        else:
            root = self._archive_root / source.source_id
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        target = root / day
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _move_to(source: Path, directory: Path) -> Path:
        """Move ``source`` into ``directory`` with collision-safe rename."""
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / source.name
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            counter = 1
            while True:
                candidate = directory / f"{stem}_{counter}{suffix}"
                if not candidate.exists():
                    destination = candidate
                    break
                counter += 1
        shutil.move(str(source), str(destination))
        return destination
