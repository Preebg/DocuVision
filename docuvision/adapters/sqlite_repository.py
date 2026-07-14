"""Embedded SQLite ``PipelineRepository`` for on-prem job state."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docuvision.core.models import DocumentJob, PipelineStatus

logger = logging.getLogger(__name__)

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL,
    local_path TEXT,
    layout_markdown TEXT,
    extracted_payload TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_file_name ON jobs(file_name);
"""


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value)


class SqlitePipelineRepository:
    """Persists ``DocumentJob`` rows in a local SQLite database."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
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
            connection.executescript(_INIT_SQL)
            connection.commit()
        finally:
            connection.close()
        logger.info("sqlite.initialized path=%s", self._db_path)

    def create_job(self, file_name: str) -> DocumentJob:
        job = DocumentJob(file_name=file_name, status=PipelineStatus.QUEUED)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, file_name, status, local_path, layout_markdown,
                    extracted_payload, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.file_name,
                    job.status.value,
                    job.local_path,
                    job.layout_markdown,
                    self._dumps(job.extracted_payload),
                    job.error_message,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
        logger.info("sqlite.create_job id=%s file_name=%s", job.id, file_name)
        return job

    def save_job(self, job: DocumentJob) -> DocumentJob:
        updated = job.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET
                    file_name = ?,
                    status = ?,
                    local_path = ?,
                    layout_markdown = ?,
                    extracted_payload = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    updated.file_name,
                    updated.status.value,
                    updated.local_path,
                    updated.layout_markdown,
                    self._dumps(updated.extracted_payload),
                    updated.error_message,
                    updated.updated_at.isoformat(),
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
    ) -> DocumentJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")

        kwargs: dict[str, Any] = {}
        if local_path is not None:
            kwargs["local_path"] = local_path
        if layout_markdown is not None:
            kwargs["layout_markdown"] = layout_markdown
        if extracted_payload is not None:
            kwargs["extracted_payload"] = extracted_payload
        if error_message is not None:
            kwargs["error_message"] = error_message
        elif status != PipelineStatus.FAILED:
            kwargs["error_message"] = None

        updated = job.transition_to(status, **kwargs)
        return self.save_job(updated)

    def mark_failed(self, job_id: str, error_message: str) -> DocumentJob:
        return self.update_status(
            job_id,
            PipelineStatus.FAILED,
            error_message=error_message,
        )

    def get_job(self, job_id: str) -> DocumentJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_by_status(self, status: PipelineStatus) -> list[DocumentJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (status.value,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    @staticmethod
    def _dumps(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _loads(raw: str | None) -> dict[str, Any] | None:
        if raw is None or raw == "":
            return None
        return json.loads(raw)

    def _row_to_job(self, row: sqlite3.Row) -> DocumentJob:
        return DocumentJob(
            id=row["job_id"],
            file_name=row["file_name"],
            status=PipelineStatus(row["status"]),
            local_path=row["local_path"],
            layout_markdown=row["layout_markdown"],
            extracted_payload=self._loads(row["extracted_payload"]),
            error_message=row["error_message"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
