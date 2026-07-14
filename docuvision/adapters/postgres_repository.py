"""PostgreSQL ``PipelineRepository`` with connection pooling (psycopg).

Schema source of truth: ``migrations/001_documents_pipeline_events.sql``.
Operational fields (local_path, layout, payload, errors, updated_at) are
persisted inside the ``documents.metadata`` JSON document so the physical
columns stay aligned with the dual-engine migration contract.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from docuvision.core.models import DocumentJob, PipelineStatus

logger = logging.getLogger(__name__)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "001_documents_pipeline_events.sql"
)

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
        migration_sql = _MIGRATION_PATH.read_text(encoding="utf-8")
        statements = [
            part.strip()
            for part in migration_sql.split(";")
            if part.strip() and not all(
                line.strip().startswith("--") or not line.strip()
                for line in part.strip().splitlines()
            )
        ]
        with self._pool.connection() as connection:
            for statement in statements:
                # Skip trailing comment-only blocks after the last semicolon.
                executable = "\n".join(
                    line
                    for line in statement.splitlines()
                    if not line.strip().startswith("--")
                ).strip()
                if executable:
                    connection.execute(executable)
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
        """Claim oldest ``QUEUED`` row via ``FOR UPDATE SKIP LOCKED`` → ``SCANNING``."""
        with self._pool.connection() as connection:
            # Non-blocking row lock: concurrent workers skip already-claimed ids.
            row = connection.execute(
                """
                WITH next_job AS (
                    SELECT id
                    FROM documents
                    WHERE status = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE documents AS d
                SET status = %s
                FROM next_job
                WHERE d.id = next_job.id
                RETURNING d.*
                """,
                (PipelineStatus.QUEUED.value, PipelineStatus.SCANNING.value),
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
