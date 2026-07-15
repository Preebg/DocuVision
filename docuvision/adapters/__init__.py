"""Concrete backends for on-prem storage, persistence, and Ollama."""

from docuvision.adapters.ingestion import IngestionWatcher, load_ingestion_sources
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.ollama_client import ModelRecoverableError, OllamaVisionClient
from docuvision.adapters.postgres_repository import PostgresPipelineRepository
from docuvision.adapters.release import FSDropExporter, ReleaseExporter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository

__all__ = [
    "FSDropExporter",
    "IngestionWatcher",
    "LocalStorageAdapter",
    "ModelRecoverableError",
    "OllamaVisionClient",
    "PostgresPipelineRepository",
    "ReleaseExporter",
    "SqlitePipelineRepository",
    "load_ingestion_sources",
]
