"""Embedded SQLite ``PipelineRepository`` for on-prem job state.

Schema source of truth: ``migrations/*.sql`` (applied in lexical order).
Operational fields (local_path, layout, payload, errors, updated_at) are
persisted inside the ``documents.metadata`` JSON document so the physical
columns stay aligned with the dual-engine migration contract.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from docuvision.adapters.schema import load_migration_scripts
from docuvision.core.models import (
    DEFAULT_BATCH_PRIORITY,
    PENDING_BATCH_STATUSES,
    Batch,
    BatchDocumentLink,
    BatchStatus,
    BatchSummary,
    DocumentJob,
    DocumentPage,
    PipelineStatus,
)

logger = logging.getLogger(__name__)

# Reserved metadata keys for operational job fields not in the DDL columns.
_META_LOCAL_PATH = "local_path"
_META_LAYOUT = "layout_markdown"
_META_PAYLOAD = "extracted_payload"
_META_ERROR = "error_message"
_META_UPDATED_AT = "updated_at"


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    # SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" (UTC, no tz).
    if "T" not in value and "+" not in value:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return datetime.fromisoformat(value)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlitePipelineRepository:
    """Persists ``DocumentJob`` rows in local ``documents`` + audit events."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        actor: str = "sqlite-worker",
    ) -> None:
        self._db_path = Path(db_path)
        self._actor = actor
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # DELETE journaling avoids lingering *-wal locks on Windows.
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            for name, migration_sql in load_migration_scripts():
                connection.executescript(migration_sql)
                logger.debug("sqlite.migration applied=%s", name)
            connection.commit()
        finally:
            connection.close()
        logger.info("sqlite.initialized path=%s", self._db_path)

    def create_job(self, file_name: str) -> DocumentJob:
        """Idempotent insert: reuse an existing row for ``filename`` if present."""
        existing = self.get_job_by_filename(file_name)
        if existing is not None:
            logger.info(
                "sqlite.create_job reuse id=%s file_name=%s status=%s retry_count=%s",
                existing.id,
                file_name,
                existing.status.value,
                existing.retry_count,
            )
            return existing

        job = DocumentJob(file_name=file_name, status=PipelineStatus.QUEUED)
        metadata = self._pack_metadata(job)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    id, filename, status, retry_count, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.file_name,
                    job.status.value,
                    job.retry_count,
                    self._dumps(metadata),
                    job.created_at.isoformat(),
                ),
            )
            self._insert_event(
                connection,
                job_id=job.id,
                action="JOB_CREATED",
                from_status=None,
                to_status=job.status.value,
                details={"filename": file_name},
            )
        logger.info("sqlite.create_job id=%s file_name=%s", job.id, file_name)
        return job

    def create_batch(
        self,
        *,
        name: str,
        priority: int = DEFAULT_BATCH_PRIORITY,
        source_id: str | None = None,
        documents: Sequence[tuple[str, int]],
    ) -> Batch:
        """Insert a batch row and ordered ``batch_documents`` membership links."""
        if not name or not str(name).strip():
            raise ValueError("create_batch requires a non-empty name.")
        if priority not in (1, 2, 3):
            raise ValueError(f"Invalid batch priority: {priority} (expected 1, 2, or 3)")
        if not documents:
            raise ValueError("create_batch requires at least one document link.")

        batch = Batch(
            name=str(name).strip(),
            priority=priority,
            status=BatchStatus.INGESTED,
            source_id=source_id,
        )
        now = batch.created_at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO batches (
                    id, name, priority, status, source_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.id,
                    batch.name,
                    batch.priority,
                    batch.status.value,
                    batch.source_id,
                    now,
                    now,
                ),
            )
            links: list[BatchDocumentLink] = []
            for document_id, sequence_no in documents:
                row = connection.execute(
                    "SELECT id, filename FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Document not found for batch link: {document_id}")
                connection.execute(
                    """
                    INSERT INTO batch_documents (batch_id, document_id, sequence_no)
                    VALUES (?, ?, ?)
                    """,
                    (batch.id, document_id, int(sequence_no)),
                )
                links.append(
                    BatchDocumentLink(
                        document_id=document_id,
                        sequence_no=int(sequence_no),
                        file_name=row["filename"],
                    )
                )
                self._insert_event(
                    connection,
                    job_id=document_id,
                    action="BATCH_LINKED",
                    from_status=None,
                    to_status=None,
                    details={
                        "batch_id": batch.id,
                        "batch_name": batch.name,
                        "sequence_no": int(sequence_no),
                        "priority": batch.priority,
                    },
                )
        links.sort(key=lambda item: item.sequence_no)
        batch.documents = links
        logger.info(
            "sqlite.create_batch id=%s name=%s priority=%s documents=%s",
            batch.id,
            batch.name,
            batch.priority,
            len(links),
        )
        return batch

    def list_pending_batches(self) -> list[BatchSummary]:
        status_values = [status.value for status in PENDING_BATCH_STATUSES]
        placeholders = ",".join("?" for _ in status_values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    b.id,
                    b.name,
                    b.priority,
                    b.status,
                    b.source_id,
                    b.created_at,
                    b.updated_at,
                    COUNT(bd.document_id) AS document_count
                FROM batches b
                LEFT JOIN batch_documents bd ON bd.batch_id = b.id
                WHERE b.status IN ({placeholders})
                GROUP BY b.id
                ORDER BY b.priority DESC, b.created_at ASC
                """,
                status_values,
            ).fetchall()
        return [self._row_to_batch_summary(row) for row in rows]

    def add_document_pages(
        self,
        document_id: str,
        pages: Sequence[tuple[int, str]],
    ) -> list[DocumentPage]:
        if self.get_job(document_id) is None:
            raise KeyError(f"Document not found: {document_id}")
        created: list[DocumentPage] = []
        with self._connect() as connection:
            for page_no, file_path in pages:
                page = DocumentPage(
                    document_id=document_id,
                    page_no=int(page_no),
                    file_path=file_path,
                )
                connection.execute(
                    """
                    INSERT INTO document_pages (id, document_id, page_no, file_path)
                    VALUES (?, ?, ?, ?)
                    """,
                    (page.id, page.document_id, page.page_no, page.file_path),
                )
                created.append(page)
        created.sort(key=lambda item: item.page_no)
        return created

    def save_job(self, job: DocumentJob) -> DocumentJob:
        updated = job.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        metadata = self._pack_metadata(updated)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE documents SET
                    filename = ?,
                    status = ?,
                    retry_count = ?,
                    metadata = ?
                WHERE id = ?
                """,
                (
                    updated.file_name,
                    updated.status.value,
                    updated.retry_count,
                    self._dumps(metadata),
                    updated.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Job not found: {updated.id}")
        return updated

    def update_status(
        self,
        job_id: str,
        status: PipelineStatus,
        *,
        local_path: str | None = None,
        layout_markdown: str | None = None,
        extracted_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")

        from_status = job.status.value
        kwargs: dict[str, Any] = {}
        if local_path is not None:
            kwargs["local_path"] = local_path
        if layout_markdown is not None:
            kwargs["layout_markdown"] = layout_markdown
        if extracted_payload is not None:
            kwargs["extracted_payload"] = extracted_payload
        if retry_count is not None:
            kwargs["retry_count"] = retry_count
        if metadata is not None:
            kwargs["metadata"] = metadata
        if error_message is not None:
            kwargs["error_message"] = error_message
        elif status != PipelineStatus.FAILED:
            kwargs["error_message"] = None

        updated = job.transition_to(status, **kwargs)
        saved = self.save_job(updated)

        with self._connect() as connection:
            self._insert_event(
                connection,
                job_id=saved.id,
                action="STAGE_TRANSITION",
                from_status=from_status,
                to_status=saved.status.value,
                details={
                    "retry_count": saved.retry_count,
                    "error_message": saved.error_message,
                },
            )
        return saved

    def mark_failed(self, job_id: str, error_message: str) -> DocumentJob:
        return self.update_status(
            job_id,
            PipelineStatus.FAILED,
            error_message=error_message,
        )

    def get_job(self, job_id: str) -> DocumentJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def get_job_by_filename(self, file_name: str) -> DocumentJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE filename = ?",
                (file_name,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def claim_next_queued(self) -> DocumentJob | None:
        """Atomically claim highest-priority ``QUEUED`` → ``SCANNING`` (SQLite CAS).

        SQLite lacks ``FOR UPDATE SKIP LOCKED``. ``BEGIN IMMEDIATE`` serializes
        writers so concurrent processes cannot pull the same row; the
        ``WHERE status = QUEUED`` compare-and-set rejects lost races.
        Batch priority (HIGH first) beats older medium/low work.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT d.*
                FROM documents d
                LEFT JOIN (
                    SELECT bd.document_id, MAX(b.priority) AS priority
                    FROM batch_documents bd
                    JOIN batches b ON b.id = bd.batch_id
                    GROUP BY bd.document_id
                ) bp ON bp.document_id = d.id
                WHERE d.status = ?
                ORDER BY COALESCE(bp.priority, ?) DESC, d.created_at ASC
                LIMIT 1
                """,
                (PipelineStatus.QUEUED.value, DEFAULT_BATCH_PRIORITY),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None

            candidate = self._row_to_job(row)
            claimed = candidate.transition_to(PipelineStatus.SCANNING)
            metadata = self._pack_metadata(claimed)
            cursor = connection.execute(
                """
                UPDATE documents SET
                    status = ?,
                    metadata = ?
                WHERE id = ? AND status = ?
                """,
                (
                    PipelineStatus.SCANNING.value,
                    self._dumps(metadata),
                    claimed.id,
                    PipelineStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None

            self._mark_batch_processing(connection, claimed.id)
            self._insert_event(
                connection,
                job_id=claimed.id,
                action="JOB_CLAIMED",
                from_status=PipelineStatus.QUEUED.value,
                to_status=PipelineStatus.SCANNING.value,
                details={"filename": claimed.file_name},
            )
            connection.commit()
            logger.info(
                "sqlite.claim_next_queued id=%s file_name=%s",
                claimed.id,
                claimed.file_name,
            )
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_batch_processing(
        self,
        connection: sqlite3.Connection,
        document_id: str,
    ) -> None:
        """Flip parent batch(es) from ``ingested`` → ``processing`` on first claim."""
        now = _utc_iso()
        connection.execute(
            """
            UPDATE batches
            SET status = ?, updated_at = ?
            WHERE status = ?
              AND id IN (
                  SELECT batch_id FROM batch_documents WHERE document_id = ?
              )
            """,
            (
                BatchStatus.PROCESSING.value,
                now,
                BatchStatus.INGESTED.value,
                document_id,
            ),
        )

    def list_by_status(self, status: PipelineStatus) -> list[DocumentJob]:
        return self.list_by_statuses([status])

    def list_by_statuses(self, statuses: list[PipelineStatus] | tuple[PipelineStatus, ...]) -> list[DocumentJob]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        values = [status.value for status in statuses]
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM documents
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                values,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def emit_audit_event(
        self,
        *,
        job_id: str,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append-only insert into ``pipeline_events`` with an explicit actor."""
        identity = (actor or "").strip()
        if not identity:
            raise ValueError("emit_audit_event requires a non-empty actor identity.")
        with self._connect() as connection:
            self._insert_event(
                connection,
                job_id=job_id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                details=details,
                actor=identity,
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        details: dict[str, Any] | None,
        actor: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pipeline_events (
                id, job_id, actor, action, from_status, to_status, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                job_id,
                actor if actor is not None else self._actor,
                action,
                from_status,
                to_status,
                self._dumps(details) if details is not None else None,
                _utc_iso(),
            ),
        )

    @staticmethod
    def _dumps(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _loads(raw: str | None) -> dict[str, Any]:
        if raw is None or raw == "":
            return {}
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            return {}
        return loaded

    def _pack_metadata(self, job: DocumentJob) -> dict[str, Any]:
        packed = dict(job.metadata)
        packed[_META_UPDATED_AT] = job.updated_at.isoformat()
        if job.local_path is not None:
            packed[_META_LOCAL_PATH] = job.local_path
        elif _META_LOCAL_PATH in packed:
            packed.pop(_META_LOCAL_PATH, None)
        if job.layout_markdown is not None:
            packed[_META_LAYOUT] = job.layout_markdown
        if job.extracted_payload is not None:
            packed[_META_PAYLOAD] = job.extracted_payload
        if job.error_message is not None:
            packed[_META_ERROR] = job.error_message
        elif _META_ERROR in packed:
            packed.pop(_META_ERROR, None)
        return packed

    def _row_to_job(self, row: sqlite3.Row) -> DocumentJob:
        meta = self._loads(row["metadata"])
        operational = {
            _META_LOCAL_PATH,
            _META_LAYOUT,
            _META_PAYLOAD,
            _META_ERROR,
            _META_UPDATED_AT,
        }
        public_meta = {k: v for k, v in meta.items() if k not in operational}
        updated_raw = meta.get(_META_UPDATED_AT)
        return DocumentJob(
            id=row["id"],
            file_name=row["filename"],
            status=PipelineStatus(row["status"]),
            retry_count=int(row["retry_count"] or 0),
            metadata=public_meta,
            local_path=meta.get(_META_LOCAL_PATH),
            layout_markdown=meta.get(_META_LAYOUT),
            extracted_payload=meta.get(_META_PAYLOAD),
            error_message=meta.get(_META_ERROR),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(updated_raw)
            if isinstance(updated_raw, str)
            else _parse_dt(row["created_at"]),
        )

    @staticmethod
    def _row_to_batch_summary(row: sqlite3.Row) -> BatchSummary:
        return BatchSummary(
            id=row["id"],
            name=row["name"],
            priority=int(row["priority"]),
            status=BatchStatus(row["status"]),
            source_id=row["source_id"],
            document_count=int(row["document_count"] or 0),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
