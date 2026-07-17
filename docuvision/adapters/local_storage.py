"""Local filesystem ``StorageBackend`` for air-gapped ingress / egress.

Directory roots may be local disks or network-attached share mounts
(SMB / NFS). Configure via ``DOCUVISION_DATA_ROOT`` or explicit
``DOCUVISION_INCOMING_DIR``, ``DOCUVISION_VERIFICATION_DIR``, etc.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import BinaryIO, Sequence

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
        release/       — enterprise share drop (Pillar 5; optional override)
    """

    def __init__(self, settings: Settings) -> None:
        self._incoming = settings.resolved_incoming_dir()
        self._workspace = settings.resolved_workspace_dir()
        self._verification = settings.resolved_verification_dir()
        self._processed = settings.resolved_processed_dir()
        self._failed = settings.resolved_failed_dir()
        self._quarantine = settings.resolved_pipeline_quarantine_dir()
        self._release = settings.resolved_release_dir()
        self.ensure_directories()

    def ensure_directories(self) -> None:
        for path in (
            self._incoming,
            self._workspace,
            self._verification,
            self._processed,
            self._failed,
            self._quarantine,
            self._release,
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

    def quarantine_payload(
        self,
        document_id: str,
        file_name: str,
        local_path: str | None,
    ) -> str:
        """Move one document beneath its opaque quarantine namespace."""
        safe_id = "".join(ch for ch in document_id if ch.isalnum() or ch in "-_")
        if not safe_id or safe_id != document_id:
            raise ValueError("Invalid document id for quarantine storage.")
        item_dir = (self._quarantine / safe_id).resolve()
        if not item_dir.is_relative_to(self._quarantine.resolve()):
            raise ValueError("Refusing quarantine path outside configured root.")
        item_dir.mkdir(parents=True, exist_ok=True)
        destination = item_dir / Path(file_name).name
        ingress = self._safe_join(self._incoming, file_name)
        workspace = Path(local_path).resolve() if local_path else None
        if workspace is not None and not workspace.is_relative_to(self._workspace.resolve()):
            workspace = None

        if destination.is_file():
            if ingress.is_file():
                ingress.unlink()
            if workspace is not None and workspace.is_file():
                workspace.unlink()
        elif ingress.is_file():
            shutil.move(str(ingress), destination)
            if workspace is not None and workspace.is_file():
                workspace.unlink()
        elif workspace is not None and workspace.is_file():
            shutil.move(str(workspace), destination)
        else:
            for root in (self._failed, self._verification, self._processed):
                candidate = self._safe_join(root, file_name)
                if candidate.is_file():
                    shutil.move(str(candidate), destination)
                    break
            else:
                raise FileNotFoundError(f"No payload found to quarantine: {file_name}")

        locator = destination.relative_to(self._quarantine).as_posix()
        logger.info(
            "storage.quarantine document_id=%s file_name=%s",
            document_id,
            file_name,
        )
        return locator

    def restore_for_redrive(
        self,
        file_name: str,
        payload_locator: str | None,
    ) -> str:
        """Restore from quarantine or legacy failed storage without overwrite."""
        destination = self._safe_join(self._incoming, file_name)
        source: Path | None = None
        if payload_locator:
            candidate = (self._quarantine / payload_locator).resolve()
            if not candidate.is_relative_to(self._quarantine.resolve()):
                raise ValueError("Refusing quarantine locator outside configured root.")
            if candidate.is_file():
                source = candidate
            elif destination.is_file():
                # The known quarantine source has already been moved.
                return destination.name
            else:
                raise FileNotFoundError(
                    f"Quarantine payload is missing: {file_name}"
                )
        else:
            for root in (self._failed, self._verification, self._processed):
                candidate = self._safe_join(root, file_name)
                if candidate.is_file():
                    source = candidate
                    break

        if destination.is_file():
            if source is not None:
                raise FileExistsError(
                    f"Incoming redrive destination already exists: {file_name}"
                )
            # Source is gone: a prior interrupted redrive already moved it.
            return destination.name
        if source is None:
            raise FileNotFoundError(f"No redrive payload found: {file_name}")
        shutil.move(str(source), destination)
        logger.info("storage.redrive restored file_name=%s", file_name)
        return destination.name

    def write_incoming(self, file_name: str, source: BinaryIO | bytes) -> str:
        """Write an uploaded document into the ingress directory.

        Returns the stored basename (may be uniquified on collision).
        """
        destination = self._unique_path(self._incoming, file_name)
        if isinstance(source, bytes):
            destination.write_bytes(source)
        else:
            with destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
        logger.info("storage.write_incoming file_name=%s path=%s", destination.name, destination)
        return destination.name

    def write_incoming_batch(
        self,
        documents: Sequence[tuple[str, BinaryIO | bytes]],
    ) -> list[str]:
        """Write an ordered list of documents into ingress; preserve sequence.

        Returns stored basenames in the same order as ``documents``.
        """
        stored: list[str] = []
        for file_name, source in documents:
            stored.append(self.write_incoming(file_name, source))
        logger.info("storage.write_incoming_batch count=%s", len(stored))
        return stored

    def resolve_document_path(
        self,
        file_name: str,
        *,
        prefer_verification: bool = True,
    ) -> Path:
        """Locate a document under verification / processed / incoming.

        Raises ``FileNotFoundError`` when no matching file exists.
        """
        if prefer_verification:
            roots = (self._verification, self._processed, self._incoming)
        else:
            roots = (self._incoming, self._verification, self._processed)
        for root in roots:
            candidate = self._safe_join(root, file_name)
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"Document not found in storage roots: {file_name}")

    def cleanup_workspace(self, local_path: str | None) -> None:
        """Remove a scratch workspace file if it still exists."""
        if not local_path:
            return
        path = Path(local_path)
        if not path.is_file():
            return
        try:
            # Only delete paths that live under the configured workspace root.
            resolved = path.resolve()
            workspace_root = self._workspace.resolve()
            if not resolved.is_relative_to(workspace_root):
                logger.warning(
                    "storage.cleanup refused path outside workspace: %s",
                    local_path,
                )
                return
            resolved.unlink(missing_ok=True)
            logger.info("storage.cleanup_workspace path=%s", resolved)
        except OSError:
            logger.exception("storage.cleanup_workspace failed path=%s", local_path)

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
