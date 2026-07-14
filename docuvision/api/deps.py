"""FastAPI dependency helpers — adapters stored on app.state by lifespan."""

from __future__ import annotations

from fastapi import Request

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.release import ReleaseExporter
from docuvision.core.contracts import PipelineRepository
from docuvision.core.release import ReleaseEngine


def get_storage(request: Request) -> LocalStorageAdapter:
    return request.app.state.storage


def get_repository(request: Request) -> PipelineRepository:
    return request.app.state.repository


def get_release_exporter(request: Request) -> ReleaseExporter:
    return request.app.state.release_exporter


def get_release_engine(request: Request) -> ReleaseEngine:
    return request.app.state.release_engine
