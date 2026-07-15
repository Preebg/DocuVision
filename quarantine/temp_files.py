"""Isolated temporary file helpers for document ingestion."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_file_stem(file_name: str) -> str:
    """Strip path components and unsafe characters from an upload name."""
    stem = Path(file_name).name
    stem = _UNSAFE_NAME_RE.sub("_", stem).strip("._")
    return stem or "document"


def create_ingestion_temp_file(file_name: str, suffix: str | None = None) -> str:
    """
    Create an isolated temp file for an incoming document.

    Uses NamedTemporaryFile(delete=False) so the path survives across
    thread hand-offs; callers MUST remove it in a finally block.
    """
    safe_stem = sanitize_file_stem(file_name)
    resolved_suffix = suffix if suffix is not None else Path(file_name).suffix
    if resolved_suffix and not resolved_suffix.startswith("."):
        resolved_suffix = f".{resolved_suffix}"

    handle = tempfile.NamedTemporaryFile(
        prefix=f"docuvision_{safe_stem}_",
        suffix=resolved_suffix or "",
        delete=False,
    )
    try:
        return handle.name
    finally:
        handle.close()


def safe_remove(path: str | None) -> None:
    """Best-effort delete; never raises."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
