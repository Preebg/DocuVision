"""Local filesystem ``StorageBackend`` for air-gapped ingress / egress."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from docuvision.config.settings import Settings

logger = logging.getLogger(__name__)

PROCESSABLE_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


class LocalStorageAdapter:
    """Maps ``StorageBackend`` operations onto configured on-prem directories.

    Directory layout (defaults under ``data_root``)::

        incoming/      — scan operator drop zone
        workspace/     — scratch copy while a job runs
        verification/  — HITL review staging
        processed/     — successful archive
        failed/        — dead-letter
    """

    def __init__(self, settings: Settings) -> None:
        self._incoming = settings.resolved_incoming_dir()
        self._workspace = settings.resolved_workspace_dir()
        self._verification = settings.resolved_verification_dir()
        self._processed = settings.resolved_processed_dir()
        self._failed = settings.resolved_failed_dir()
        self.ensure_directories()

    def ensure_directories(self) -> None:
        for path in (
            self._incoming,
            self._workspace,
            self._verification,
            self._processed,
            self._failed,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def list_incoming(self) -> list[str]:
        if not self._incoming.is_dir():
            return []

        names: list[str] = []
        for entry in sorted(self._incoming.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                continue
            if entry.suffix.lower() not in PROCESSABLE_SUFFIXES:
                continue
            names.append(entry.name)
        return names

    def fetch_to_workspace(self, file_name: str) -> str:
        """Copy ingress file into workspace; leave original until mark_*."""
        source = self._safe_join(self._incoming, file_name)
        if not source.is_file():
            raise FileNotFoundError(f"Incoming file not found: {source}")

        destination = self._unique_path(self._workspace, file_name)
        shutil.copy2(source, destination)
        logger.info("storage.fetch file_name=%s workspace=%s", file_name, destination)
        return str(destination)

    def stage_for_verification(self, file_name: str, local_path: str) -> str:
        destination = self._unique_path(self._verification, file_name)
        shutil.copy2(local_path, destination)
        logger.info(
            "storage.stage file_name=%s verification=%s",
            file_name,
            destination,
        )
        return str(destination)

    def mark_processed(self, file_name: str, local_path: str) -> None:
        self._archive(file_name, local_path, self._processed, label="processed")

    def mark_failed(self, file_name: str, local_path: str | None) -> None:
        source = Path(local_path) if local_path else self._safe_join(self._incoming, file_name)
        if not source.is_file():
            # Prefer moving the ingress original if workspace copy is gone.
            ingress = self._safe_join(self._incoming, file_name)
            source = ingress if ingress.is_file() else source
        self._archive(file_name, str(source) if source.is_file() else None, self._failed, label="failed")

    def _archive(
        self,
        file_name: str,
        local_path: str | None,
        archive_dir: Path,
        *,
        label: str,
    ) -> None:
        destination = self._unique_path(archive_dir, file_name)

        moved = False
        ingress = self._safe_join(self._incoming, file_name)
        if ingress.is_file():
            shutil.move(str(ingress), destination)
            moved = True
            logger.info("storage.%s moved_ingress=%s -> %s", label, ingress, destination)

        if local_path:
            workspace = Path(local_path)
            if workspace.is_file():
                if not moved:
                    shutil.move(str(workspace), destination)
                    moved = True
                    logger.info(
                        "storage.%s moved_workspace=%s -> %s",
                        label,
                        workspace,
                        destination,
                    )
                else:
                    workspace.unlink(missing_ok=True)

        if not moved:
            logger.warning(
                "storage.%s nothing to archive for file_name=%s",
                label,
                file_name,
            )

    @staticmethod
    def _safe_join(directory: Path, file_name: str) -> Path:
        """Prevent path traversal; only allow a basename under ``directory``."""
        candidate = (directory / Path(file_name).name).resolve()
        root = directory.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"Refusing path outside storage root: {file_name}")
        return candidate

    @staticmethod
    def _unique_path(directory: Path, file_name: str) -> Path:
        base = Path(file_name).name
        candidate = directory / base
        if not candidate.exists():
            return candidate

        stem = Path(base).stem
        suffix = Path(base).suffix
        counter = 1
        while True:
            candidate = directory / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1
