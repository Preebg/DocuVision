"""Abstract ``ReleaseExporter`` contract for air-gapped batch egress."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ReleaseExporter(ABC):
    """Egress adapter: write a verified batch package to an isolated share.

    Implementations must be local / LAN-only — no cloud SDKs. Disk I/O and
    network-mount failures should raise ``ReleaseExportError`` (or ``OSError``)
    so the orchestrator can transition the batch to ``release_failed`` without
    crashing the worker loop.
    """

    @abstractmethod
    async def export_batch(self, batch_id: str, payload: dict[str, Any]) -> bool:
        """Export ``payload`` for ``batch_id`` to the configured release target.

        Returns ``True`` on success. On failure, raise a catchable exception
        rather than returning ``False`` unless the failure is intentionally soft.
        """
        ...
