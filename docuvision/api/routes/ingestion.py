"""Pillar 1 ingestion health / heartbeat endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from docuvision.adapters.ingestion.config import load_ingestion_sources
from docuvision.adapters.ingestion.status_store import IngestionStatusStore
from docuvision.api.schemas import IngestionSourceStatusOut, IngestionStatusResponse
from docuvision.config.settings import Settings

router = APIRouter(tags=["ingestion"])


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/api/ingestion/status", response_model=IngestionStatusResponse)
def ingestion_status(request: Request) -> IngestionStatusResponse:
    """Return health, online status, and queue depth for all declared sources."""
    settings: Settings = request.app.state.settings
    status_path = settings.resolved_ingestion_status_path()
    snapshot = IngestionStatusStore.read_file(status_path)

    configured = _configured_source_stubs(settings)
    if snapshot is None:
        return IngestionStatusResponse(
            running=False,
            online=False,
            stale=True,
            total_queue_depth=0,
            sources=configured,
        )

    updated_at = _parse_iso(snapshot.get("updated_at") if isinstance(snapshot, dict) else None)
    now = datetime.now(timezone.utc)
    stale = True
    if updated_at is not None:
        age = (now - updated_at.astimezone(timezone.utc)).total_seconds()
        stale = age > settings.ingestion_status_stale_seconds

    running = bool(snapshot.get("running")) and not stale
    raw_sources = snapshot.get("sources") or []
    by_id = {
        item["source_id"]: item
        for item in raw_sources
        if isinstance(item, dict) and item.get("source_id")
    }

    sources_out: list[IngestionSourceStatusOut] = []
    total_queue = 0
    any_online = False
    for stub in configured:
        live = by_id.get(stub.source_id)
        if live is None:
            sources_out.append(stub)
            continue
        online = bool(live.get("online")) and running
        if online:
            any_online = True
        queue_depth = int(live.get("queue_depth") or 0)
        total_queue += queue_depth
        health = str(live.get("health") or ("healthy" if online else "offline"))
        if not running:
            health = "offline"
            online = False
        sources_out.append(
            IngestionSourceStatusOut(
                source_id=stub.source_id,
                display_name=str(live.get("display_name") or stub.display_name),
                type=str(live.get("type") or stub.type),
                path=str(live.get("path") or stub.path),
                online=online,
                health=health,
                queue_depth=queue_depth,
                last_poll_at=live.get("last_poll_at"),
                last_ingest_at=live.get("last_ingest_at"),
                last_error=live.get("last_error"),
                batches_formed=int(live.get("batches_formed") or 0),
                files_ingested=int(live.get("files_ingested") or 0),
                files_quarantined=int(live.get("files_quarantined") or 0),
                listener=str(live.get("listener") or "none"),
            )
        )

    return IngestionStatusResponse(
        daemon=str(snapshot.get("daemon") or "ingestion-watcher"),
        running=running,
        online=any_online and running,
        started_at=snapshot.get("started_at"),
        updated_at=snapshot.get("updated_at"),
        stale=stale,
        total_queue_depth=total_queue,
        sources=sources_out,
    )


def _configured_source_stubs(settings: Settings) -> list[IngestionSourceStatusOut]:
    path = settings.resolved_ingestion_sources_path()
    try:
        sources = load_ingestion_sources(path, data_root=settings.data_root)
    except (FileNotFoundError, ValueError, OSError):
        return []
    return [
        IngestionSourceStatusOut(
            source_id=item.source_id,
            display_name=item.display_name,
            type=item.type,
            path=str(item.path),
            online=False,
            health="unknown",
            queue_depth=0,
            listener="none",
        )
        for item in sources
    ]
