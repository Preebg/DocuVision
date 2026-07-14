"""Launch the local FastAPI HITL service.

Usage::

    python run_api.py
    # or
    uvicorn docuvision.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import sys


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import uvicorn

    from docuvision.config.settings import get_settings

    settings = get_settings()
    settings.ensure_directories()
    uvicorn.run(
        "docuvision.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
