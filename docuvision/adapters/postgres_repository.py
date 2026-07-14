"""PostgreSQL ``PipelineRepository`` with connection pooling (psycopg).

Schema source of truth: ``migrations/*.sql`` (applied in lexical order).
Operational fields (local_path, layout, payload, errors, updated_at) are
persisted inside the ``documents.metadata`` JSON document so the physical
columns stay aligned with the dual-engine migration contract.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from docuvision.adapters.schema import load_migration_scripts, split_sql_statements
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

_META_LOCAL_PATH = "local_path"
_META_LAYOUT = "layout_markdown"
_META_PAYLOAD = "extracted_payload"
_META_ERROR = "error_message"
_META_UPDATED_AT = "updated_at"


def _parse_dt(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
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


class PostgresPipelineRepository:
    """Persists ``DocumentJob`` rows in Postgres ``documents`` + audit events."""

    def __init__(
        self,
        database_url: str,
        *,
        actor: str = "postgres-worker",
        min_size: int = 1,
        max_size: int = 8,
    ) -> None:
        self._actor = actor
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._initialize()

    def close(self) -> None:
        self._pool.close()

    def _initialize(self) -> None:
        with self._pool.connection() as connection:
            for name, migration_sql in load_migration_scripts():
                for statement in split_sql_statements(migration_sql):
                    connection.execute(statement)
                logger.debug("postgres.migration applied=%s", name)
            connection.commit()
        logger.info("postgres.initialized")

    def create_job(self, file_name: str) -> DocumentJob:
        """Idempotent insert: reuse an existing row for ``filename`` if present."""
        existing = self.get_job_by_filename(file_name)
        if existing is not None:
            logger.info(
                "postgres.create_job reuse id=%s file_name=%s status=%s retry_count=%s",
                existing.id,
                file_name,
                existing.status.value,
                existing.retry_count,
            )
            return existing

        job = DocumentJob(file_name=file_name, status=PipelineStatus.QUEUED)
        metadata = self._pack_metadata(job)
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    id, filename, status, retry_count, metadata, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
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
            connection.commit()
        logger.info("postgres.create_job id=%s file_name=%s", job.id, file_name)
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
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO batches (
                    id, name, priority, status, source_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                    "SELECT id, filename FROM documents WHERE id = %s",
                    (document_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Document not found for batch link: {document_id}")
                connection.execute(
                    """
                    INSERT INTO batch_documents (batch_id, document_id, sequence_no)
                    VALUES (%s, %s, %s)
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
            connection.commit()
        links.sort(key=lambda item: item.sequence_no)
        batch.documents = links
        logger.info(
            "postgres.create_batch id=%s name=%s priority=%s documents=%s",
            batch.id,
            batch.name,
            batch.priority,
            len(links),
        )
        return batch

    def list_pending_batches(self) -> list[BatchSummary]:
        status_values = [status.value for status in PENDING_BATCH_STATUSES]
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
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
                WHERE b.status = ANY(%s)
                GROUP BY b.id
                ORDER BY b.priority DESC, b.created_at ASC
                """,
                (status_values,),
            ).fetchall()
        return [self._row_to_batch_summary(row) for row in rows]

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM batches WHERE id = %s",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            links = self._load_batch_links(connection, batch_id)
        batch = self._row_to_batch(row)
        batch.documents = links
        return batch

    def list_batches_for_document(self, document_id: str) -> list[Batch]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT b.*
                FROM batches b
                INNER JOIN batch_documents bd ON bd.batch_id = b.id
                WHERE bd.document_id = %s
                ORDER BY b.created_at ASC
                """,
                (document_id,),
            ).fetchall()
            batches: list[Batch] = []
            for row in rows:
                batch = self._row_to_batch(row)
                batch.documents = self._load_batch_links(connection, batch.id)
                batches.append(batch)
        return batches

    def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Batch:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"Batch not found: {batch_id}")
        from_status = batch.status.value
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = %s, updated_at = %s
                WHERE id = %s
                """,
                (status.value, _utc_iso(), batch_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Batch not found: {batch_id}")
            if batch.documents:
                self._insert_event(
                    connection,
                    job_id=batch.documents[0].document_id,
                    action="BATCH_STATUS_TRANSITION",
                    from_status=from_status,
                    to_status=status.value,
                    details={
                        "batch_id": batch_id,
                        "batch_name": batch.name,
                        **(details or {}),
                    },
                    actor=actor if actor is not None else self._actor,
                )
            connection.commit()
        updated = self.get_batch(batch_id)
        if updated is None:
            raise KeyError(f"Batch not found after update: {batch_id}")
        logger.info(
            "postgres.update_batch_status id=%s %s→%s",
            batch_id,
            from_status,
            status.value,
        )
        return updated

    def list_document_pages(self, document_id: str) -> list[DocumentPage]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, document_id, page_no, file_path
                FROM document_pages
                WHERE document_id = %s
                ORDER BY page_no ASC
                """,
                (document_id,),
            ).fetchall()
        return [
            DocumentPage(
                id=row["id"],
                document_id=row["document_id"],
                page_no=int(row["page_no"]),
                file_path=row["file_path"],
            )
            for row in rows
        ]

    def maybe_mark_batches_ready_for_hitl(self, document_id: str) -> list[Batch]:
        advanced: list[Batch] = []
        for batch in self.list_batches_for_document(document_id):
            if batch.status != BatchStatus.PROCESSING:
                continue
            if not batch.documents:
                continue
            ready = True
            for link in batch.documents:
                job = self.get_job(link.document_id)
                if job is None or job.status.value not in {
                    PipelineStatus.PENDING_VERIFICATION.value,
                    PipelineStatus.VERIFIED.value,
                }:
                    ready = False
                    break
            if not ready:
                continue
            advanced.append(
                self.update_batch_status(
                    batch.id,
                    BatchStatus.READY_FOR_HITL,
                    actor=self._actor,
                    details={"trigger_document_id": document_id},
                )
            )
        return advanced

    def add_document_pages(
        self,
        document_id: str,
        pages: Sequence[tuple[int, str]],
    ) -> list[DocumentPage]:
        if self.get_job(document_id) is None:
            raise KeyError(f"Document not found: {document_id}")
        created: list[DocumentPage] = []
        with self._pool.connection() as connection:
            for page_no, file_path in pages:
                page = DocumentPage(
                    document_id=document_id,
                    page_no=int(page_no),
                    file_path=file_path,
                )
                connection.execute(
                    """
                    INSERT INTO document_pages (id, document_id, page_no, file_path)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (page.id, page.document_id, page.page_no, page.file_path),
                )
                created.append(page)
            connection.commit()
        created.sort(key=lambda item: item.page_no)
        return created

    def save_job(self, job: DocumentJob) -> DocumentJob:
        updated = job.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        metadata = self._pack_metadata(updated)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE documents SET
                    filename = %s,
                    status = %s,
                    retry_count = %s,
                    metadata = %s
                WHERE id = %s
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
            connection.commit()
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

        with self._pool.connection() as connection:
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
            connection.commit()
        return saved

    def mark_failed(self, job_id: str, error_message: str) -> DocumentJob:
        return self.update_status(
            job_id,
            PipelineStatus.FAILED,
            error_message=error_message,
        )

    def get_job(self, job_id: str) -> DocumentJob | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = %s",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def get_job_by_filename(self, file_name: str) -> DocumentJob | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE filename = %s",
                (file_name,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def claim_next_queued(self) -> DocumentJob | None:
        """Claim highest-priority ``QUEUED`` via ``FOR UPDATE SKIP LOCKED`` → ``SCANNING``."""
        with self._pool.connection() as connection:
            # Non-blocking row lock: concurrent workers skip already-claimed ids.
            # Prefer HIGH batch priority; standalone docs use DEFAULT_BATCH_PRIORITY.
            row = connection.execute(
                """
                WITH next_job AS (
                    SELECT d.id
                    FROM documents d
                    LEFT JOIN (
                        SELECT bd.document_id, MAX(b.priority) AS priority
                        FROM batch_documents bd
                        JOIN batches b ON b.id = bd.batch_id
                        GROUP BY bd.document_id
                    ) bp ON bp.document_id = d.id
                    WHERE d.status = %s
                    ORDER BY COALESCE(bp.priority, %s) DESC, d.created_at ASC
                    LIMIT 1
                    FOR UPDATE OF d SKIP LOCKED
                )
                UPDATE documents AS d
                SET status = %s
                FROM next_job
                WHERE d.id = next_job.id
                RETURNING d.*
                """,
                (
                    PipelineStatus.QUEUED.value,
                    DEFAULT_BATCH_PRIORITY,
                    PipelineStatus.SCANNING.value,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            claimed = self._row_to_job(row)
            # Persist updated_at inside metadata JSON (operational clock).
            stamped = claimed.transition_to(PipelineStatus.SCANNING)
            metadata = self._pack_metadata(stamped)
            connection.execute(
                "UPDATE documents SET metadata = %s WHERE id = %s",
                (self._dumps(metadata), stamped.id),
            )
            self._mark_batch_processing(connection, stamped.id)
            self._insert_event(
                connection,
                job_id=stamped.id,
                action="JOB_CLAIMED",
                from_status=PipelineStatus.QUEUED.value,
                to_status=PipelineStatus.SCANNING.value,
                details={"filename": stamped.file_name},
            )
            connection.commit()
            logger.info(
                "postgres.claim_next_queued id=%s file_name=%s",
                stamped.id,
                stamped.file_name,
            )
            return stamped

    def _mark_batch_processing(self, connection: Any, document_id: str) -> None:
        """Flip parent batch(es) from ``ingested`` → ``processing`` on first claim."""
        connection.execute(
            """
            UPDATE batches
            SET status = %s, updated_at = %s
            WHERE status = %s
              AND id IN (
                  SELECT batch_id FROM batch_documents WHERE document_id = %s
              )
            """,
            (
                BatchStatus.PROCESSING.value,
                _utc_iso(),
                BatchStatus.INGESTED.value,
                document_id,
            ),
        )

    def list_by_status(self, status: PipelineStatus) -> list[DocumentJob]:
        return self.list_by_statuses([status])

    def list_by_statuses(self, statuses: Sequence[PipelineStatus]) -> list[DocumentJob]:
        if not statuses:
            return []
        values = [status.value for status in statuses]
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM documents
                WHERE status = ANY(%s)
                ORDER BY created_at ASC
                """,
                (values,),
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
        with self._pool.connection() as connection:
            self._insert_event(
                connection,
                job_id=job_id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                details=details,
                actor=identity,
            )
            connection.commit()

    def _insert_event(
        self,
        connection: Any,
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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
    def _loads(raw: str | dict[str, Any] | None) -> dict[str, Any]:
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
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

    def _row_to_job(self, row: dict[str, Any]) -> DocumentJob:
        meta = self._loads(row.get("metadata"))
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
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(updated_raw)
            if isinstance(updated_raw, (str, datetime))
            else _parse_dt(row.get("created_at")),
        )

    @staticmethod
    def _row_to_batch_summary(row: dict[str, Any]) -> BatchSummary:
        return BatchSummary(
            id=row["id"],
            name=row["name"],
            priority=int(row["priority"]),
            status=BatchStatus(row["status"]),
            source_id=row.get("source_id"),
            document_count=int(row.get("document_count") or 0),
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("updated_at")),
        )

    @staticmethod
    def _row_to_batch(row: dict[str, Any]) -> Batch:
        return Batch(
            id=row["id"],
            name=row["name"],
            priority=int(row["priority"]),
            status=BatchStatus(row["status"]),
            source_id=row.get("source_id"),
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("updated_at")),
            documents=[],
        )

    @staticmethod
    def _load_batch_links(connection: Any, batch_id: str) -> list[BatchDocumentLink]:
        rows = connection.execute(
            """
            SELECT bd.document_id, bd.sequence_no, d.filename
            FROM batch_documents bd
            INNER JOIN documents d ON d.id = bd.document_id
            WHERE bd.batch_id = %s
            ORDER BY bd.sequence_no ASC
            """,
            (batch_id,),
        ).fetchall()
        return [
            BatchDocumentLink(
                document_id=row["document_id"],
                sequence_no=int(row["sequence_no"]),
                file_name=row["filename"],
            )
            for row in rows
        ]
