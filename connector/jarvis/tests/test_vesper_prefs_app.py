"""Dashboard preferences that follow the user to every device (/vesper/prefs), and the home-screen app built on them
(manifest, persona icons, service worker, and the page starting as the user left it)."""

import html
import json
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def page_prefs(page):
    return json.loads(html.unescape(re.search(r'data-vesper-prefs="([^"]*)"', page).group(1)))


@pytest.mark.asyncio
async def test_prefs_are_saved_merged_and_shared_with_the_page(hermes_home):
    async with TestClient(TestServer(app())) as client:
        assert (await client.get("/vesper/prefs")).status == 401
        assert (await (await client.get("/vesper/prefs", headers=AUTH)).json()) == {"prefs": {}}
        resp = await client.put("/vesper/prefs", headers=AUTH, json={
            "persona": "ultron", "model": {"provider": "anthropic", "id": "claude-opus-5-5"}, "clock12": True})
        assert resp.status == 200
        resp = await client.put("/vesper/prefs", headers=AUTH, json={"tempUnit": "F", "clock12": None, "voiceEngine": "pocket"})
        saved = (await resp.json())["prefs"]
        assert saved == {"persona": "ultron", "model": {"provider": "anthropic", "id": "claude-opus-5-5"}, "tempUnit": "F", "voiceEngine": "pocket"}
        # what a second device sees, from the API and on the page itself
        assert (await (await client.get("/vesper/prefs", headers=AUTH)).json())["prefs"] == saved
        page = await (await client.get("/vesper")).text()
    assert page_prefs(page) == saved
    assert json.loads((hermes_home / "vesper-prefs.json").read_text()) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"nope": 1}, {"layout": "sideways"}, {"clock12": "yes"},
                                  {"model": {"provider": "anthropic"}}, {"session": "../../etc"}, {"voiceEngine": "bark"}, ["persona"]])
async def test_bad_prefs_are_refused_and_change_nothing(hermes_home, body):
    async with TestClient(TestServer(app())) as client:
        await client.put("/vesper/prefs", headers=AUTH, json={"persona": "cortana"})
        resp = await client.put("/vesper/prefs", headers=AUTH, json=body)
        assert resp.status == 400
        assert (await (await client.get("/vesper/prefs", headers=AUTH)).json())["prefs"] == {"persona": "cortana"}


@pytest.mark.asyncio
async def test_a_hand_edited_prefs_file_only_yields_valid_values(hermes_home):
    (hermes_home / "vesper-prefs.json").write_text(json.dumps({"persona": "cortana", "layout": 7, "extra": "x"}))
    async with TestClient(TestServer(app())) as client:
        assert (await (await client.get("/vesper/prefs", headers=AUTH)).json())["prefs"] == {"persona": "cortana"}


@pytest.mark.asyncio
async def test_the_home_screen_app_takes_the_saved_persona(hermes_home):
    async with TestClient(TestServer(app())) as client:
        page = await (await client.get("/vesper")).text()
        assert 'href="/vesper/manifest.webmanifest?persona=jarvis"' in page and "/vesper/icons/jarvis-180.png" in page
        await client.put("/vesper/prefs", headers=AUTH, json={"persona": "cortana"})
        resp = await client.get("/vesper")
        page = await resp.text()
        assert "manifest-src 'self'" in resp.headers["Content-Security-Policy"]
        assert 'href="/vesper/manifest.webmanifest?persona=cortana"' in page
        assert 'href="/vesper/icons/cortana-180.png"' in page and 'href="/vesper/icons/cortana-32.png"' in page
        assert '<meta name="apple-mobile-web-app-title" content="Cortana">' in page

        # no ?persona: the saved one; an explicit one wins; every listed icon is a real PNG
        default = await (await client.get("/vesper/manifest.webmanifest")).json()
        assert default["name"] == "Cortana" and default["start_url"] == "/vesper" and default["display"] == "standalone"
        explicit = await (await client.get("/vesper/manifest.webmanifest?persona=ultron")).json()
        assert explicit["name"] == "Ultron" and explicit["id"] == default["id"]
        for entry in explicit["icons"]:
            resp = await client.get(entry["src"])
            assert resp.status == 200 and resp.content_type == "image/png"
            assert (await resp.read())[:8] == b"\x89PNG\r\n\x1a\n"
        assert {i["purpose"] for i in explicit["icons"]} == {"any", "maskable"}
        assert (await client.get("/vesper/icons/../../api_server.py")).status == 404
        assert (await client.get("/vesper/icons/someone-512.png")).status == 404


@pytest.mark.asyncio
async def test_the_service_worker_only_handles_navigation_and_never_caches():
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/sw.js")
        body = await resp.text()
    assert resp.status == 200 and resp.content_type == "application/javascript"
    assert resp.headers["Service-Worker-Allowed"] == "/vesper"
    assert "caches." not in body and "event.request.mode !== 'navigate'" in body
