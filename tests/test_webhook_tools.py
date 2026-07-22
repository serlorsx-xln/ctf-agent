"""webhook.site helpers — accept 201 Created as success."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.tools.core import do_webhook_create, do_webhook_get_requests


def _json_response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


@pytest.mark.asyncio
async def test_webhook_create_accepts_201():
    client = AsyncMock()
    client.post = AsyncMock(
        return_value=_json_response(201, {"uuid": "abc-123"}),
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.tools.core.httpx.AsyncClient", return_value=client):
        out = await do_webhook_create()

    assert "abc-123" in out
    assert "webhook.site/abc-123" in out
    assert "error" not in out.lower()


@pytest.mark.asyncio
async def test_webhook_create_rejects_500():
    client = AsyncMock()
    client.post = AsyncMock(return_value=_json_response(500, {}))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.tools.core.httpx.AsyncClient", return_value=client):
        out = await do_webhook_create()

    assert out.startswith("webhook.site error: HTTP 500")


@pytest.mark.asyncio
async def test_webhook_get_requests_empty():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_json_response(200, {"data": []}))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.tools.core.httpx.AsyncClient", return_value=client):
        out = await do_webhook_get_requests("abc-123")

    assert out == "No requests received yet."
