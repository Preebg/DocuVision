"""Filesystem drop exporter for air-gapped enterprise network shares.

Reads ``DOCUVISION_RELEASE_DIR`` (or an injected path) and writes a
deterministic package per batch::

    {RELEASE_DIR}/{batch_name}_{batch_id}/
        01_doc.pdf
        02_doc.pdf
        metadata.json
        checksums.sha256
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docuvision.adapters.release.base import ReleaseExporter
from docuvision.core.errors import ReleaseExportError

logger = logging.getLogger(__name__)

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9._-]+")
_ENV_RELEASE_DIR = "DOCUVISION_RELEASE_DIR"


class FSDropExporter(ReleaseExporter):
    """Production filesystem / SMB-mount drop adapter (Pillar 5).

    Triggers on the verified → releasing → released path. All I/O errors
    become ``ReleaseExportError`` so orchestrators can mark ``release_failed``.
    """

    def __init__(self, release_dir: Path | str | None = None) -> None:
        resolved = self._resolve_release_root(release_dir)
        self._release_dir = resolved
        try:
            self._release_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReleaseExportError(
                f"Cannot create release root '{self._release_dir}': {exc}"
            ) from exc

    @property
    def release_dir(self) -> Path:
        return self._release_dir

    async def export_batch(self, batch_id: str, payload: dict[str, Any]) -> bool:
        """Export on a worker thread so mount latency never blocks the event loop."""
        try:
            return await asyncio.to_thread(self._export_batch_sync, batch_id, payload)
        except ReleaseExportError:
            raise
        except OSError as exc:
            raise ReleaseExportError(
                f"Release export I/O failed for batch {batch_id}: {exc}"
            ) from exc
        except Exception as exc:
            raise ReleaseExportError(
                f"Release export failed for batch {batch_id}: {exc}"
            ) from exc

    def _export_batch_sync(self, batch_id: str, payload: dict[str, Any]) -> bool:
        batch_meta = payload.get("batch") if isinstance(payload.get("batch"), dict) else {}
        batch_name = str(batch_meta.get("name") or payload.get("batch_name") or "batch")
        documents = payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ReleaseExportError(
                f"Release payload for batch {batch_id} has no documents."
            )

        package_dir = self._package_dir(batch_name, batch_id)
        # Fresh package on every attempt (retry-safe).
        if package_dir.exists():
            shutil.rmtree(package_dir)
        try:
            package_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise ReleaseExportError(
                f"Cannot create package directory '{package_dir}': {exc}"
            ) from exc

        written_files: list[Path] = []
        try:
            export_rows: list[dict[str, Any]] = []
            for index, document in enumerate(documents, start=1):
                if not isinstance(document, dict):
                    raise ReleaseExportError(
                        f"Invalid document entry at index {index} for batch {batch_id}."
                    )
                pdf_path = self._write_document_pdf(
                    package_dir, index, document, batch_id=batch_id
                )
                written_files.append(pdf_path)
                export_rows.append(
                    {
                        "document_id": document.get("document_id"),
                        "sequence_no": document.get("sequence_no", index),
                        "file_name": document.get("file_name"),
                        "exported_pdf": pdf_path.name,
                        "extracted_payload": document.get("extracted_payload"),
                        "verified_payload": document.get("verified_payload"),
                        "verified_by": document.get("verified_by"),
                        "fields": document.get("fields") or {},
                    }
                )

            metadata = {
                "batch_id": batch_id,
                "batch_name": batch_name,
                "priority": batch_meta.get("priority"),
                "source_id": batch_meta.get("source_id"),
                "released_at": datetime.now(timezone.utc).isoformat(),
                "document_count": len(export_rows),
                "documents": export_rows,
            }
            metadata_path = package_dir / "metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            written_files.append(metadata_path)

            checksum_path = package_dir / "checksums.sha256"
            checksum_path.write_text(
                self._build_checksum_manifest(written_files, package_dir),
                encoding="utf-8",
            )
            # Integrity seal (checksums of PDFs + metadata only) — not listed
            # inside itself to keep the manifest stable for auditors.

            logger.info(
                "release.fs_drop.success batch_id=%s package=%s files=%s",
                batch_id,
                package_dir,
                len(written_files) + 1,
            )
            return True
        except Exception:
            # Never leave a half-written package on the auditor share.
            if package_dir.exists():
                shutil.rmtree(package_dir, ignore_errors=True)
            raise

    def _write_document_pdf(
        self,
        package_dir: Path,
        index: int,
        document: dict[str, Any],
        *,
        batch_id: str,
    ) -> Path:
        file_name = str(document.get("file_name") or f"document_{index}")
        stem = _SAFE_COMPONENT.sub("_", Path(file_name).stem).strip("._") or f"doc_{index}"
        destination = package_dir / f"{index:02d}_{stem}.pdf"

        page_paths = document.get("page_paths")
        source_path = document.get("source_path")
        sources: list[Path] = []
        if isinstance(page_paths, list) and page_paths:
            sources = [Path(str(p)) for p in page_paths]
        elif source_path:
            sources = [Path(str(source_path))]
        else:
            raise ReleaseExportError(
                f"Document '{file_name}' in batch {batch_id} has no source_path "
                "or page_paths for PDF export."
            )

        for path in sources:
            if not path.is_file():
                raise ReleaseExportError(
                    f"Source file missing for batch {batch_id} document "
                    f"'{file_name}': {path}"
                )

        try:
            self._write_cleaned_pdf(sources, destination)
        except ReleaseExportError:
            raise
        except OSError as exc:
            raise ReleaseExportError(
                f"Failed writing PDF for '{file_name}' in batch {batch_id}: {exc}"
            ) from exc
        except Exception as exc:
            raise ReleaseExportError(
                f"PDF merge/clean failed for '{file_name}' in batch {batch_id}: {exc}"
            ) from exc
        return destination

    @staticmethod
    def _write_cleaned_pdf(sources: list[Path], destination: Path) -> None:
        """Merge page fragments (if any) and rewrite as a cleaned PDF via PyMuPDF."""
        import fitz

        output = fitz.open()
        try:
            for source in sources:
                suffix = source.suffix.lower()
                if suffix == ".pdf":
                    with fitz.open(source) as src:
                        output.insert_pdf(src)
                elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
                    with fitz.open(source) as img_doc:
                        pdf_bytes = img_doc.convert_to_pdf()
                    with fitz.open("pdf", pdf_bytes) as converted:
                        output.insert_pdf(converted)
                else:
                    raise ReleaseExportError(
                        f"Unsupported release source type: {source.suffix}"
                    )
            destination.parent.mkdir(parents=True, exist_ok=True)
            output.save(destination, garbage=4, deflate=True, clean=True)
        finally:
            output.close()

    @staticmethod
    def _build_checksum_manifest(files: list[Path], package_dir: Path) -> str:
        lines: list[str] = []
        for path in sorted(files, key=lambda p: p.name.lower()):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = path.relative_to(package_dir).as_posix()
            lines.append(f"{digest}  {relative}")
        return "\n".join(lines) + "\n"

    def _package_dir(self, batch_name: str, batch_id: str) -> Path:
        safe_name = _SAFE_COMPONENT.sub("_", batch_name).strip("._") or "batch"
        safe_id = _SAFE_COMPONENT.sub("_", batch_id).strip("._") or "unknown"
        return self._release_dir / f"{safe_name}_{safe_id}"

    @staticmethod
    def _resolve_release_root(release_dir: Path | str | None) -> Path:
        if release_dir is not None:
            return Path(release_dir)
        env_value = os.environ.get(_ENV_RELEASE_DIR)
        if env_value and str(env_value).strip():
            return Path(str(env_value).strip())
        raise ReleaseExportError(
            f"{_ENV_RELEASE_DIR} is not configured. Set the environment "
            "variable or pass release_dir to FSDropExporter."
        )
