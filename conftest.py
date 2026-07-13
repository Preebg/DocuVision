"""Pytest bootstrap — stub heavy optional deps so unit collection stays offline."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Docling pulls large native stacks; unit tests never need the real converter.
if "docling" not in sys.modules:
    _docling = MagicMock()
    sys.modules["docling"] = _docling
    sys.modules["docling.document_converter"] = MagicMock()
