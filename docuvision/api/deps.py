"""FastAPI dependency helpers — adapters stored on app.state by lifespan."""

from __future__ import annotations

from fastapi import Request

from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.core.contracts import PipelineRepository


def get_storage(request: Request) -> LocalStorageAdapter:
    return request.app.state.storage


def get_repository(request: Request) -> PipelineRepository:
    return request.app.state.repository
