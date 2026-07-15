"""YAML loader for ``config/ingestion_sources.yaml``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

SourceType = Literal["local_directory", "network_share_poll"]
VALID_PRIORITIES = frozenset({1, 2, 3})
VALID_TYPES = frozenset({"local_directory", "network_share_poll"})


@dataclass(frozen=True, slots=True)
class IngestionSourceConfig:
    """One autonomous drop target declared in the sources registry."""

    source_id: str
    display_name: str
    type: SourceType
    path: Path
    batch_priority: int
    polling_interval_seconds: int
    file_filters: tuple[str, ...]
    quarantine_path: Path
    batch_window_seconds: int = 5
    archive_path: Path | None = None

    def matches(self, file_path: Path) -> bool:
        """Return True when the path suffix is allowed by ``file_filters``."""
        suffix = file_path.suffix.lower()
        if not suffix:
            return False
        normalized = {item if item.startswith(".") else f".{item}" for item in self.file_filters}
        return suffix in {item.lower() for item in normalized}


def default_ingestion_sources_path() -> Path:
    """``<repo>/config/ingestion_sources.yaml`` relative to this package."""
    # docuvision/adapters/ingestion/config.py → parents[3] = repo root
    return Path(__file__).resolve().parents[3] / "config" / "ingestion_sources.yaml"


def resolve_configured_path(raw: str | Path, data_root: Path | None = None) -> Path:
    """Resolve YAML paths for Docker Linux and Windows host runs.

    Rules:
    * ``/data/...`` maps onto ``data_root`` (so the same YAML works in Compose
      and on a Windows laptop without watching ``C:\\data\\...`` by accident).
    * Relative paths join ``data_root`` when provided.
    * Other absolute paths (``/mnt/...``, ``D:\\scans``) are kept as-is.
    """
    text = str(raw).strip().replace("\\", "/")
    root = Path(data_root) if data_root is not None else None

    if root is not None and (text == "/data" or text.startswith("/data/")):
        suffix = text[len("/data") :].lstrip("/")
        return (root / suffix).resolve() if suffix else root.resolve()

    path = Path(str(raw))
    if not path.is_absolute() and root is not None:
        return (root / path).resolve()
    return path


def load_ingestion_sources(
    path: Path | str | None = None,
    *,
    data_root: Path | str | None = None,
) -> list[IngestionSourceConfig]:
    """Parse and validate the multi-source ingestion registry."""
    config_path = Path(path) if path is not None else default_ingestion_sources_path()
    if not config_path.is_file():
        raise FileNotFoundError(f"Ingestion sources config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Ingestion sources root must be a mapping: {config_path}")

    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"ingestion_sources.yaml must declare a non-empty 'sources' list: {config_path}")

    root = Path(data_root) if data_root is not None else None
    seen_ids: set[str] = set()
    sources: list[IngestionSourceConfig] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"sources[{index}] must be a mapping")
        source = _parse_source(item, index=index, data_root=root)
        if source.source_id in seen_ids:
            raise ValueError(f"Duplicate source_id: {source.source_id}")
        seen_ids.add(source.source_id)
        sources.append(source)

    logger.info(
        "ingestion.config_loaded path=%s source_count=%s data_root=%s",
        config_path,
        len(sources),
        root,
    )
    return sources


def _parse_source(
    item: dict[str, Any],
    *,
    index: int,
    data_root: Path | None = None,
) -> IngestionSourceConfig:
    source_id = str(item.get("source_id") or "").strip()
    if not source_id:
        raise ValueError(f"sources[{index}].source_id is required")

    display_name = str(item.get("display_name") or source_id).strip()
    source_type = str(item.get("type") or "").strip()
    if source_type not in VALID_TYPES:
        raise ValueError(
            f"sources[{index}].type must be one of {sorted(VALID_TYPES)}, got {source_type!r}"
        )

    path_raw = item.get("path")
    if not path_raw:
        raise ValueError(f"sources[{index}].path is required")

    priority = int(item.get("batch_priority", 2))
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"sources[{index}].batch_priority must be 1, 2, or 3")

    poll = int(item.get("polling_interval_seconds", 5))
    if poll < 1:
        raise ValueError(f"sources[{index}].polling_interval_seconds must be >= 1")

    filters_raw = item.get("file_filters") or [".pdf", ".tiff"]
    if not isinstance(filters_raw, list) or not filters_raw:
        raise ValueError(f"sources[{index}].file_filters must be a non-empty list")
    file_filters = tuple(str(ext).strip().lower() for ext in filters_raw if str(ext).strip())

    quarantine_raw = item.get("quarantine_path")
    if not quarantine_raw:
        raise ValueError(f"sources[{index}].quarantine_path is required")

    window = int(item.get("batch_window_seconds", 5))
    if window < 1:
        raise ValueError(f"sources[{index}].batch_window_seconds must be >= 1")

    archive_raw = item.get("archive_path")
    archive_path = (
        resolve_configured_path(archive_raw, data_root) if archive_raw else None
    )

    return IngestionSourceConfig(
        source_id=source_id,
        display_name=display_name,
        type=source_type,  # type: ignore[arg-type]
        path=resolve_configured_path(path_raw, data_root),
        batch_priority=priority,
        polling_interval_seconds=poll,
        file_filters=file_filters,
        quarantine_path=resolve_configured_path(quarantine_raw, data_root),
        batch_window_seconds=window,
        archive_path=archive_path,
    )
