"""A bare visit to the gateway port lands on the dashboard (Dockge's port links point at '/')."""

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
async def test_root_redirects_to_dashboard():
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302
        target = resp.headers["Location"]
        followed = await client.get("/", allow_redirects=True)
        assert followed.status == 200 and str(followed.url).endswith("/vesper") and target == "vesper"
