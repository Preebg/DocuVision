"""Pillar 1 — sole active local / network-share ingestion daemon.

Usage (on-prem)::

    set DOCUVISION_DATA_ROOT=C:\\Projects\\DocuVision\\data
    set DOCUVISION_INGESTION_SOURCES_PATH=C:\\Projects\\DocuVision\\config\\ingestion_sources.yaml
    python run_ingestion.py

Watches sources declared in ``config/ingestion_sources.yaml``, packages stable
files into Batches (Migration 002), and writes a heartbeat for
``GET /api/ingestion/status``. Persistence is local SQLite at
``{data_root}/docuvision.db`` (no cloud sync).

The retired Supabase cloud watcher lives under ``quarantine/watcher.py`` and
must not be started. The pipeline worker (``run_worker.py``) claims queued
jobs from the shared ingress directory.
"""

from __future__ import annotations

import logging
import sys


def main(argv: list[str] | None = None) -> int:
    _ = argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("docuvision.ingestion")

    from docuvision.adapters.ingestion.watcher import run_ingestion_daemon
    from docuvision.config.settings import get_settings

    settings = get_settings()
    logger.info("=== DocuVision ingestion daemon (Pillar 1) ===")
    logger.info("data_root=%s", settings.data_root)
    logger.info("sources=%s", settings.resolved_ingestion_sources_path())
    logger.info("archive=%s", settings.resolved_ingestion_archive_dir())
    logger.info("status=%s", settings.resolved_ingestion_status_path())
    if settings.database_url:
        logger.info("repository=postgres")
    else:
        logger.info("repository=sqlite path=%s", settings.resolved_sqlite_path())

    try:
        run_ingestion_daemon(settings)
    except KeyboardInterrupt:
        logger.info("ingestion stopped")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
