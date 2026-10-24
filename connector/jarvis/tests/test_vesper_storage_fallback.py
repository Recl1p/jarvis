"""/vesper/storage.js: the storage fallback is served and loads before every other dashboard script."""

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


@pytest.mark.asyncio
async def test_storage_fallback_is_served_without_auth():
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/storage.js")
        assert resp.status == 200
        assert resp.content_type == "application/javascript"
        body = await resp.text()
        assert "defineProperty(window, name" in body and "localStorage" in body


@pytest.mark.asyncio
async def test_dashboard_loads_the_fallback_first():
    async with TestClient(TestServer(app())) as client:
        page = await (await client.get("/vesper")).text()
    scripts = re.findall(r'<script src="([^"]+)"', page)
    assert scripts[0] == "/vesper/storage.js", scripts
    assert "/vesper/support.js" in scripts
