"""DocuVision local FastAPI service — HITL queue and document detail endpoints.

Usage::

    python run_api.py

The live SQLite schema (see ``migrations/001_documents_pipeline_events.sql``)
stores operational fields in ``documents.metadata`` JSON:

* ``local_path`` → exposed as ``file_path``
* ``extracted_payload`` → exposed as ``extracted_json``

``extracted_payload`` is a nested JSON object. Vision extraction stores rich
per-field entries (``value``, ``bbox_2d``, ``normalized_bbox``, ``confidence``)
inside ``extracted_payload.fields``; HITL verify/read paths must keep that
structure as a real object (never double-encoded as a string).

HITL API aliases map onto pipeline contract statuses:

* ``VERIFYING`` → ``PENDING_VERIFICATION``
* ``APPROVED`` → ``VERIFIED``
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import uvicorn

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = Path(r"C:\Projects\DocuVision\data\docuvision.db")

# Friendly HITL names → values in contracts/pipeline-status.json
_STATUS_TO_DB: dict[str, str] = {
    "VERIFYING": "PENDING_VERIFICATION",
    "APPROVED": "VERIFIED",
}

_META_LOCAL_PATH = "local_path"
_META_PAYLOAD = "extracted_payload"
_META_VERIFIED = "verified_payload"


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection with Row factory (dict-like rows)."""
    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # DELETE journaling avoids lingering *-wal locks on Windows.
    connection.execute("PRAGMA journal_mode = DELETE")
    return connection


def _resolve_db_status(status: str) -> str:
    """Map API-facing status names onto persisted pipeline statuses."""
    return _STATUS_TO_DB.get(status, status)


def _loads_json_object(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _parse_extracted_json(value: Any) -> Any:
    """Return extracted payload as a dict/list, never a double-encoded string.

    Preserves nested rich field objects::

        fields.invoice_number = {
            "value": "...",
            "bbox_2d": [...],
            "normalized_bbox": [...],
            "confidence": 0.98,
        }
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        # Unwrap a second layer if present (legacy double serialization).
        if isinstance(parsed, str):
            try:
                nested = json.loads(parsed)
            except json.JSONDecodeError:
                return parsed
            return nested
        return parsed
    return value


def _file_path_from_metadata(metadata: dict[str, Any]) -> str | None:
    path = metadata.get(_META_LOCAL_PATH)
    return path if isinstance(path, str) and path else None


def _list_item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _loads_json_object(row["metadata"])
    return {
        "id": row["id"],
        "filename": row["filename"],
        "file_path": _file_path_from_metadata(metadata),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _detail_from_row(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _loads_json_object(row["metadata"])
    # Keep nested value/bbox/confidence objects intact for the PDF overlay.
    extracted = _parse_extracted_json(metadata.get(_META_PAYLOAD))
    return {
        "id": row["id"],
        "filename": row["filename"],
        "status": row["status"],
        "retry_count": row["retry_count"],
        "metadata": metadata,
        "created_at": row["created_at"],
        "file_path": _file_path_from_metadata(metadata),
        "extracted_json": extracted,
    }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class VerifyRequest(BaseModel):
    corrected_data: dict[str, Any] = Field(
        ...,
        description="Clerk-corrected extraction payload to persist.",
    )


class VerifyResponse(BaseModel):
    status: str = "success"
    id: str
    document_status: str


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def create_hitl_app() -> FastAPI:
    """Build the full DocuVision API and attach HITL queue/detail routes."""
    from docuvision.api.main import create_app
    from docuvision.config.settings import get_settings

    settings = get_settings()
    settings.ensure_directories()
    # Keep sqlite path aligned with this service's DB helper.
    if settings.database_url is None:
        settings.sqlite_path = DB_PATH

    app = create_app()

    # Outermost CORS: allow React/Tauri hosts without origin blockages.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/documents")
    def list_documents(
        status: str = Query(default="VERIFYING"),
    ) -> list[dict[str, Any]]:
        """Return documents matching ``status`` (default: VERIFYING queue)."""
        db_status = _resolve_db_status(status)
        connection = get_connection()
        try:
            cursor = connection.execute(
                """
                SELECT id, filename, status, created_at, metadata
                FROM documents
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (db_status,),
            )
            rows = cursor.fetchall()
            return [_list_item_from_row(row) for row in rows]
        finally:
            connection.close()

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str) -> dict[str, Any]:
        """Return the full document row with ``extracted_json`` as an object."""
        connection = get_connection()
        try:
            cursor = connection.execute(
                """
                SELECT id, filename, status, retry_count, metadata, created_at
                FROM documents
                WHERE id = ?
                """,
                (doc_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Document not found.")
            return _detail_from_row(row)
        finally:
            connection.close()

    @app.post("/api/documents/{doc_id}/verify", response_model=VerifyResponse)
    def verify_document(doc_id: str, body: VerifyRequest) -> VerifyResponse:
        """Persist clerk corrections and transition the document to APPROVED."""
        approved_status = _resolve_db_status("APPROVED")
        connection = get_connection()
        try:
            cursor = connection.execute(
                """
                SELECT id, filename, status, retry_count, metadata, created_at
                FROM documents
                WHERE id = ?
                """,
                (doc_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Document not found.")

            metadata = _loads_json_object(row["metadata"])
            corrected = body.corrected_data
            # Persist corrected extraction for both pipeline keys used by HITL.
            # corrected_data is a nested dict — serialize once via metadata JSON
            # so value / normalized_bbox / confidence survive round-trips.
            if not isinstance(corrected, dict):
                raise HTTPException(
                    status_code=400,
                    detail="corrected_data must be a JSON object.",
                )
            metadata[_META_PAYLOAD] = corrected
            metadata[_META_VERIFIED] = corrected

            connection.execute(
                """
                UPDATE documents
                SET metadata = ?, status = ?
                WHERE id = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False),
                    approved_status,
                    doc_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        # Final release: export corrected JSON, archive PDF, mark RELEASED.
        # Run after the verify connection is closed to avoid Windows locks.
        from release_manager import release_document

        release_result = release_document(doc_id)

        return VerifyResponse(
            status="success",
            id=doc_id,
            document_status=release_result["status"],
        )

    return app


app = create_hitl_app()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
