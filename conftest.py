"""Pytest bootstrap — stub heavy optional deps so unit collection stays offline."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Stable local API handshake key for TestClient calls (see LOCAL_API_HEADERS).
os.environ.setdefault("LOCAL_API_KEY", "test-local-api-key")

LOCAL_API_HEADERS = {"X-API-Key": os.environ["LOCAL_API_KEY"]}

# Docling pulls large native stacks; unit tests never need the real converter.
if "docling" not in sys.modules:
    _docling = MagicMock()
    sys.modules["docling"] = _docling
    sys.modules["docling.document_converter"] = MagicMock()
