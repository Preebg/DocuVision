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


_PRIORITY_LABELS: dict[int, str] = {
    1: "Low",
    2: "Medium",
    3: "High",
}
_PENDING_DB_STATUS = "PENDING_VERIFICATION"


def _priority_label(priority: int | None) -> str:
    if priority is None:
        return "Medium"
    return _PRIORITY_LABELS.get(int(priority), "Medium")


def _batch_display_number(batch_id: str) -> str:
    """Compact operator-facing batch tag (e.g. Batch #A1B2C3)."""
    compact = batch_id.replace("-", "").upper()
    return compact[:6] if compact else batch_id[:8]


def _batch_nav_for_document(
    connection: sqlite3.Connection,
    doc_id: str,
) -> dict[str, Any]:
    """Resolve sequence-based prev/next IDs within the document's batch."""
    empty = {
        "batch_id": None,
        "batch_name": None,
        "batch_display": None,
        "sequence_no": None,
        "batch_document_count": None,
        "prev_document_id": None,
        "next_document_id": None,
    }
    membership = connection.execute(
        """
        SELECT
            bd.batch_id AS batch_id,
            bd.sequence_no AS sequence_no,
            b.name AS batch_name
        FROM batch_documents bd
        INNER JOIN batches b ON b.id = bd.batch_id
        WHERE bd.document_id = ?
        ORDER BY b.created_at ASC
        LIMIT 1
        """,
        (doc_id,),
    ).fetchone()
    if membership is None:
        return empty

    batch_id = str(membership["batch_id"])
    peers = connection.execute(
        """
        SELECT document_id, sequence_no
        FROM batch_documents
        WHERE batch_id = ?
        ORDER BY sequence_no ASC, document_id ASC
        """,
        (batch_id,),
    ).fetchall()
    if not peers:
        return empty

    ids = [str(row["document_id"]) for row in peers]
    try:
        index = ids.index(doc_id)
    except ValueError:
        return empty

    sequence_no = int(peers[index]["sequence_no"])
    # Next pending after this document (for auto-advance after Approve).
    next_pending_id: str | None = None
    for peer_id in ids[index + 1 :]:
        status_row = connection.execute(
            "SELECT status FROM documents WHERE id = ?",
            (peer_id,),
        ).fetchone()
        if status_row and status_row["status"] == _PENDING_DB_STATUS:
            next_pending_id = peer_id
            break

    return {
        "batch_id": batch_id,
        "batch_name": membership["batch_name"],
        "batch_display": _batch_display_number(batch_id),
        "sequence_no": sequence_no,
        "batch_document_count": len(peers),
        "prev_document_id": ids[index - 1] if index > 0 else None,
        "next_document_id": ids[index + 1] if index + 1 < len(ids) else None,
        "next_pending_document_id": next_pending_id,
    }


def _list_pending_batches(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Unique batches with ≥1 document still in PENDING_VERIFICATION."""
    rows = connection.execute(
        """
        SELECT
            b.id AS batch_id,
            b.name AS name,
            b.priority AS priority,
            COUNT(bd.document_id) AS total_documents,
            SUM(
                CASE WHEN d.status = ? THEN 1 ELSE 0 END
            ) AS pending_documents,
            MIN(d.created_at) AS created_at
        FROM batches b
        INNER JOIN batch_documents bd ON bd.batch_id = b.id
        INNER JOIN documents d ON d.id = bd.document_id
        GROUP BY b.id
        HAVING SUM(CASE WHEN d.status = ? THEN 1 ELSE 0 END) > 0
        ORDER BY b.priority DESC, MIN(d.created_at) ASC
        """,
        (_PENDING_DB_STATUS, _PENDING_DB_STATUS),
    ).fetchall()

    batches: list[dict[str, Any]] = []
    for row in rows:
        batch_id = str(row["batch_id"])
        first_pending = connection.execute(
            """
            SELECT bd.document_id AS document_id
            FROM batch_documents bd
            INNER JOIN documents d ON d.id = bd.document_id
            WHERE bd.batch_id = ?
              AND d.status = ?
            ORDER BY bd.sequence_no ASC, d.created_at ASC
            LIMIT 1
            """,
            (batch_id, _PENDING_DB_STATUS),
        ).fetchone()
        batches.append(
            {
                "batch_id": batch_id,
                "name": row["name"],
                "priority": _priority_label(row["priority"]),
                "total_documents": int(row["total_documents"] or 0),
                "pending_documents": int(row["pending_documents"] or 0),
                "created_at": row["created_at"],
                "first_pending_document_id": (
                    str(first_pending["document_id"]) if first_pending else None
                ),
                "batch_display": _batch_display_number(batch_id),
            }
        )
    return batches


def _strip_route(app: FastAPI, path: str, methods: set[str]) -> None:
    """Remove a previously registered route so HITL handlers can replace it."""
    kept = []
    for route in app.router.routes:
        route_path = getattr(route, "path", None)
        route_methods = set(getattr(route, "methods", None) or set())
        if route_path == path and route_methods.intersection(methods):
            continue
        kept.append(route)
    app.router.routes = kept


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

    # Replace the supervisor-oriented pending summary with the HITL batch queue.
    _strip_route(app, "/api/batches/pending", {"GET"})

    @app.get("/api/batches/pending")
    def list_pending_batches() -> list[dict[str, Any]]:
        """Batches with at least one document awaiting clerk verification."""
        connection = get_connection()
        try:
            return _list_pending_batches(connection)
        finally:
            connection.close()

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
        """Return document detail plus batch sequence navigation IDs."""
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
            detail = _detail_from_row(row)
            detail.update(_batch_nav_for_document(connection, doc_id))
            return detail
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
