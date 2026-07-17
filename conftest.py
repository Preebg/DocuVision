"""Pytest bootstrap — stub heavy optional deps so unit collection stays offline."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Stable local API handshake key for TestClient calls (see LOCAL_API_HEADERS).
os.environ.setdefault("LOCAL_API_KEY", "test-local-api-key")

LOCAL_API_HEADERS = {"X-API-Key": os.environ["LOCAL_API_KEY"]}


def operator_auth_headers(
    client,
    username: str = "operator_test",
) -> tuple[dict[str, str], str]:
    from docuvision.api.operator_auth import provision_operator

    operator = provision_operator(
        username,
        "correct-horse-battery-staple",
        display_name="Test Operator",
    )
    response = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": "correct-horse-battery-staple",
        },
        headers=LOCAL_API_HEADERS,
    )
    assert response.status_code == 200, response.text
    return (
        {
            **LOCAL_API_HEADERS,
            "Authorization": f"Bearer {response.json()['access_token']}",
        },
        operator.id,
    )

# Docling pulls large native stacks; unit tests never need the real converter.
if "docling" not in sys.modules:
    _docling = MagicMock()
    sys.modules["docling"] = _docling
    sys.modules["docling.document_converter"] = MagicMock()
