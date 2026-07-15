"""Pillar 1 — advanced local / network-share ingestion adapters."""

from docuvision.adapters.ingestion.config import (
    IngestionSourceConfig,
    load_ingestion_sources,
)
from docuvision.adapters.ingestion.status_store import IngestionStatusStore
from docuvision.adapters.ingestion.watcher import IngestionWatcher

__all__ = [
    "IngestionSourceConfig",
    "IngestionStatusStore",
    "IngestionWatcher",
    "load_ingestion_sources",
]
