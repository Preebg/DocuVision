"""Shared composition root — wire storage + repository for worker and API."""

from __future__ import annotations

import logging

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.pdf_batch_splitter import PyMuPdfBatchSplitter
from docuvision.adapters.postgres_repository import PostgresPipelineRepository
from docuvision.adapters.release import FSDropExporter, ReleaseExporter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.config.settings import Settings
from docuvision.core.contracts import PipelineRepository
from docuvision.core.batch_splitting import BatchSplitEngine
from docuvision.core.release import ReleaseEngine

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


def build_release_exporter(settings: Settings) -> ReleaseExporter:
    """Wire the local / network-share filesystem drop exporter."""
    release_dir = settings.resolved_release_dir()
    logger.info("composition.release_exporter=fs_drop dir=%s", release_dir)
    return FSDropExporter(release_dir)


def build_batch_split_engine(
    storage: LocalStorageAdapter,
    repository: PipelineRepository,
) -> BatchSplitEngine:
    """Compose the physical batch-separator service."""
    return BatchSplitEngine(
        repository=repository,
        storage=storage,
        splitter=PyMuPdfBatchSplitter(),
    )


def build_release_engine(
    settings: Settings,
    storage: LocalStorageAdapter,
    repository: PipelineRepository,
    *,
    exporter: ReleaseExporter | None = None,
) -> ReleaseEngine:
    """Compose the Pillar 5 release orchestrator."""
    return ReleaseEngine(
        repository=repository,
        storage=storage,
        exporter=exporter or build_release_exporter(settings),
    )
