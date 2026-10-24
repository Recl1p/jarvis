"""/vesper/core.js: the persona core graphics are served like the other dashboard scripts and loaded by the page."""

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
async def test_core_script_is_served_and_loaded_by_the_dashboard():
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/core.js")
        assert resp.status == 200 and resp.content_type == "application/javascript"
        assert "customElements.define('vesper-core'" in await resp.text()
        page = await (await client.get("/vesper")).text()
    assert "/vesper/core.js" in re.findall(r'<script src="([^"]+)"', page)
    assert "<vesper-core" in page
