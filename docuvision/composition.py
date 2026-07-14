"""Shared composition root — wire storage + repository for worker and API."""

from __future__ import annotations

import logging

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.postgres_repository import PostgresPipelineRepository
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.config.settings import Settings
from docuvision.core.contracts import PipelineRepository

logger = logging.getLogger(__name__)


def build_backends(
    settings: Settings,
) -> tuple[LocalStorageAdapter, PipelineRepository]:
    """Inject filesystem storage and the configured pipeline repository.

    If ``DOCUVISION_DATABASE_URL`` is set, use pooled Postgres; otherwise
    fall back to embedded SQLite under the data root.
    """
    settings.ensure_directories()
    storage = LocalStorageAdapter(settings)

    if settings.database_url:
        logger.info("composition.repository=postgres")
        repository: PipelineRepository = PostgresPipelineRepository(
            settings.database_url,
            actor="docuvision",
        )
    else:
        sqlite_path = settings.resolved_sqlite_path()
        logger.info("composition.repository=sqlite path=%s", sqlite_path)
        repository = SqlitePipelineRepository(sqlite_path, actor="docuvision")

    return storage, repository
