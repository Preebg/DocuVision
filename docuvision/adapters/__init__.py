"""Concrete backends for on-prem storage, persistence, and Ollama."""

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.ollama_client import ModelRecoverableError, OllamaVisionClient
from docuvision.adapters.postgres_repository import PostgresPipelineRepository
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository

__all__ = [
    "LocalStorageAdapter",
    "ModelRecoverableError",
    "OllamaVisionClient",
    "PostgresPipelineRepository",
    "SqlitePipelineRepository",
]
