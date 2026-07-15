"""Final release pipeline — export verified JSON and archive source PDFs.

Invoked after HITL approval transitions a document to ``VERIFIED``. This
module performs the terminal ``VERIFIED`` → ``RELEASED`` handoff:

1. Export the clerk-corrected JSON payload to ``{data_root}/exports``
2. Archive the original PDF from ``metadata.local_path`` to ``{data_root}/archive``
3. Persist status ``RELEASED`` in SQLite
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATUS_VERIFIED = "VERIFIED"
STATUS_RELEASED = "RELEASED"

_META_LOCAL_PATH = "local_path"
_META_PAYLOAD = "extracted_payload"
_META_VERIFIED = "verified_payload"


def _data_root() -> Path:
    """Resolve the clean on-prem data root (never hardcoded)."""
    from docuvision.config.settings import get_settings

    return get_settings().data_root


def db_path() -> Path:
    return _data_root() / "docuvision.db"


def export_dir() -> Path:
    return _data_root() / "exports"


def archive_dir() -> Path:
    return _data_root() / "archive"


def ensure_directories() -> None:
    """Create export and archive directories when missing."""
    export_dir().mkdir(parents=True, exist_ok=True)
    archive_dir().mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Open a clean SQLite connection with Row factory."""
    connection = sqlite3.connect(str(db_path()), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # DELETE journaling avoids lingering *-wal locks on Windows.
    connection.execute("PRAGMA journal_mode = DELETE")
    return connection


def _loads_json_object(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _corrected_payload(metadata: dict[str, Any]) -> Any:
    """Prefer the HITL-verified payload; fall back to extracted_payload."""
    if _META_VERIFIED in metadata:
        return metadata[_META_VERIFIED]
    return metadata.get(_META_PAYLOAD)


def _safe_filename(name: str) -> str:
    """Strip path separators so export names cannot escape the target dir."""
    return Path(name).name.replace("\\", "_").replace("/", "_")


def _export_json(doc_id: str, filename: str, payload: Any) -> Path:
    ensure_directories()
    safe_name = _safe_filename(filename)
    export_path = export_dir() / f"{doc_id}_{safe_name}.json"
    export_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("release.export path=%s", export_path)
    return export_path


def _archive_pdf(local_path: str | None, filename: str) -> Path | None:
    """Copy the source PDF into the archive directory.

    Uses ``copy2`` (not move) so Windows share/locks on the workspace file
    do not abort the release. Archive I/O errors are logged and swallowed
    so a successful JSON export can still complete the status transition.
    """
    if not local_path:
        logger.warning("release.archive skipped: metadata.local_path is empty")
        return None

    source = Path(local_path)
    if not source.is_file():
        logger.warning("release.archive skipped: source missing path=%s", source)
        return None

    ensure_directories()
    dest_name = _safe_filename(filename) if filename else source.name
    # Prefer keeping the original extension when filename has none.
    if not Path(dest_name).suffix and source.suffix:
        dest_name = f"{dest_name}{source.suffix}"
    destination = archive_dir() / dest_name

    # Avoid clobbering an existing archive entry for a distinct document.
    if destination.exists() and destination.resolve() != source.resolve():
        stem = destination.stem
        suffix = destination.suffix
        destination = archive_dir() / f"{stem}_{source.stat().st_mtime_ns}{suffix}"

    try:
        shutil.copy2(source, destination)
        logger.info("release.archive copied src=%s dest=%s", source, destination)
        return destination
    except OSError as exc:
        # Windows: PermissionError / WinError 32 (file in use), etc.
        logger.warning(
            "release.archive failed src=%s dest=%s error=%s",
            source,
            destination,
            exc,
        )
        return None


def release_document(doc_id: int | str) -> dict[str, Any]:
    """Export, archive, and mark a verified document as ``RELEASED``.

    Parameters
    ----------
    doc_id:
        Document primary key. Accepted as ``int`` or ``str`` so callers
        (HITL API UUID strings or numeric test ids) both work.
    """
    ensure_directories()
    doc_key = str(doc_id)

    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            SELECT id, filename, status, metadata
            FROM documents
            WHERE id = ?
            """,
            (doc_key,),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"Document not found: {doc_key}")

        metadata = _loads_json_object(row["metadata"])
        filename = row["filename"] or f"{doc_key}.pdf"
        payload = _corrected_payload(metadata)

        export_path = _export_json(doc_key, filename, payload)
        archive_path = _archive_pdf(
            metadata.get(_META_LOCAL_PATH)
            if isinstance(metadata.get(_META_LOCAL_PATH), str)
            else None,
            filename,
        )

        connection.execute(
            """
            UPDATE documents
            SET status = ?
            WHERE id = ? AND status = ?
            """,
            (STATUS_RELEASED, doc_key, STATUS_VERIFIED),
        )
        # If already past VERIFIED (retry), still force RELEASED.
        if connection.total_changes == 0:
            connection.execute(
                """
                UPDATE documents
                SET status = ?
                WHERE id = ?
                """,
                (STATUS_RELEASED, doc_key),
            )
        connection.commit()

        result = {
            "id": doc_key,
            "status": STATUS_RELEASED,
            "export_path": str(export_path),
            "archive_path": str(archive_path) if archive_path else None,
        }
        logger.info(
            "release.complete doc_id=%s export=%s archive=%s",
            doc_key,
            result["export_path"],
            result["archive_path"],
        )
        return result
    finally:
        connection.close()
