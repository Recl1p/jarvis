"""/vesper/radio: a fixed station allowlist, each with streams tried best first (SomaFM 404s 256k on most channels)."""

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from jarvis import core_routes
from gateway.platforms.api_server import APIServerAdapter


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


class _Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


class _Upstream:
    def __init__(self, status, body=b""):
        self.status, self.content, self.released = status, _Content(body), False

    def release(self):
        self.released = True


@pytest.fixture
def upstream(monkeypatch):
    """Fake upstream: `routes` maps URL -> status (200 streams b"MP3"), or an exception to raise."""
    state = {"routes": {}, "asked": [], "responses": []}

    class FakeSession:
        async def get(self, url, timeout=None):
            state["asked"].append(url)
            outcome = state["routes"].get(url, 404)
            if isinstance(outcome, Exception):
                raise outcome
            resp = _Upstream(outcome, b"MP3" if outcome == 200 else b"")
            state["responses"].append(resp)
            return resp

        async def close(self):
            state["closed"] = True

    state["install"] = lambda: monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return state


def test_every_station_has_a_128k_stream():
    for key, urls in core_routes.STATIONS.items():
        assert isinstance(urls, tuple) and urls, key
        assert any(u.endswith(f"/{key}-128-mp3") for u in urls), key
        assert all(u.startswith("https://ice") and ".somafm.com/" in u for u in urls), key


@pytest.mark.asyncio
async def test_falls_back_to_the_128k_stream(upstream):
    async with TestClient(TestServer(app())) as client:
        upstream["routes"]["https://ice1.somafm.com/deepspaceone-128-mp3"] = 200
        upstream["install"]()
        resp = await client.get("/vesper/radio?station=deepspaceone")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/mpeg"
        assert await resp.read() == b"MP3"
    assert upstream["asked"] == ["https://ice1.somafm.com/deepspaceone-128-mp3"]


@pytest.mark.asyncio
async def test_prefers_256k_and_releases_failed_attempts(upstream):
    async with TestClient(TestServer(app())) as client:
        upstream["routes"]["https://ice1.somafm.com/groovesalad-256-mp3"] = 404
        upstream["routes"]["https://ice1.somafm.com/groovesalad-128-mp3"] = aiohttp.ClientConnectionError("reset")
        upstream["routes"]["https://ice2.somafm.com/groovesalad-128-mp3"] = 200
        upstream["install"]()
        resp = await client.get("/vesper/radio?station=groovesalad")
        assert resp.status == 200 and await resp.read() == b"MP3"
    assert upstream["asked"] == ["https://ice1.somafm.com/groovesalad-256-mp3", "https://ice1.somafm.com/groovesalad-128-mp3",
                                 "https://ice2.somafm.com/groovesalad-128-mp3"]
    assert upstream["responses"][0].released


@pytest.mark.asyncio
async def test_all_streams_down_is_a_502(upstream):
    async with TestClient(TestServer(app())) as client:
        upstream["install"]()
        resp = await client.get("/vesper/radio?station=lush")
        assert resp.status == 502
        body = await resp.json()
        assert body["error"]["code"] == "radio_failed" and "404" in body["error"]["message"]
    assert len(upstream["asked"]) == 2 and upstream.get("closed")


@pytest.mark.asyncio
async def test_unknown_station_is_rejected(upstream):
    async with TestClient(TestServer(app())) as client:
        upstream["install"]()
        resp = await client.get("/vesper/radio?station=https://evil.example/stream")
        assert resp.status == 400
    assert upstream["asked"] == []
