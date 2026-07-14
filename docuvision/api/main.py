"""FastAPI application — HITL queue API sharing worker adapters.

Run::

    uvicorn docuvision.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from docuvision.adapters.postgres_repository import PostgresPipelineRepository
from docuvision.api.routes import batches, documents, queue
from docuvision.api.schemas import HealthResponse
from docuvision.composition import build_backends
from docuvision.config.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    storage, repository = build_backends(settings)
    app.state.settings = settings
    app.state.storage = storage
    app.state.repository = repository
    app.state.repository_kind = "postgres" if settings.database_url else "sqlite"
    logger.info(
        "api.ready repository=%s data_root=%s",
        app.state.repository_kind,
        settings.data_root,
    )
    try:
        yield
    finally:
        repo = getattr(app.state, "repository", None)
        if isinstance(repo, PostgresPipelineRepository):
            repo.close()
        logger.info("api.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="DocuVision Local API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(queue.router)
    app.include_router(documents.router)
    app.include_router(batches.router)

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            repository=getattr(app.state, "repository_kind", "unknown"),
        )

    return app


app = create_app()
