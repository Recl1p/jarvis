"""The routes a browser opens without the API key (the dashboard page, downloads, audio, the panels' status) answer
only requests the JARVIS launcher vouched for or that carry the key, and downloads never reach JARVIS's private files
(the browser desktop's logins, the launcher's login and state, the voice pack) or Hermes's credential stores."""

import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from jarvis import wiring

KEY = "test-vesper-key"
VOUCHED = {wiring.LAUNCHER_HEADER: KEY}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def wired_app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    app = web.Application()
    wiring.wire_api_server(app, adapter)
    return app


@pytest.mark.asyncio
async def test_the_dashboard_and_its_open_routes_need_the_launcher_or_the_key(home):
    async with TestClient(TestServer(wired_app())) as client:
        for path in ("/vesper", "/vesper/", "/vesper/status", "/vesper/weather", "/vesper/radio?station=lush",
                     "/vesper/files?path=/etc/hostname", "/vesper/music/tracks/" + "a" * 32):
            resp = await client.get(path)
            assert resp.status == 403, path
            assert (await resp.json())["error"]["code"] == "open_jarvis"
        assert (await client.get("/vesper", headers={wiring.LAUNCHER_HEADER: "a wrong key"})).status == 403
        page = await client.get("/vesper", headers=VOUCHED)
        assert page.status == 200 and f'data-vesper-key="{KEY}"' in await page.text()
        assert (await client.get("/vesper", headers={"Authorization": f"Bearer {KEY}"})).status == 200
        # The static parts hold nothing private and stay open (a manifest is fetched without cookies).
        for path in ("/vesper/support.js", "/vesper/manifest.webmanifest", "/vesper/sw.js"):
            assert (await client.get(path)).status == 200, path


@pytest.mark.asyncio
async def test_downloads_never_reach_private_files(home):
    workspace = home / "workspace"
    workspace.mkdir()
    (workspace / "report.pdf").write_bytes(b"%PDF report")
    (home / ".env").write_text("API_SERVER_KEY=secret\n")
    (home / "jarvis").mkdir()
    (home / "jarvis" / "auth.json").write_text("{}")
    (home / "openjarvis" / "browser" / "Default").mkdir(parents=True)
    (home / "openjarvis" / "browser" / "Default" / "Cookies").write_bytes(b"sqlite")
    (home / "vesper-voices" / "pack").mkdir(parents=True)
    (home / "vesper-voices" / "pack" / "jarvis.safetensors").write_bytes(b"state")
    os.symlink(home / ".env", workspace / "notes.txt")
    os.symlink(home / "openjarvis" / "browser" / "Default" / "Cookies", workspace / "cookies.db")
    async with TestClient(TestServer(wired_app())) as client:
        resp = await client.get(f"/vesper/files?path={workspace / 'report.pdf'}", headers=VOUCHED)
        assert resp.status == 200 and await resp.read() == b"%PDF report"
        for target in (home / ".env", home / "jarvis" / "auth.json", home / "openjarvis" / "browser" / "Default" / "Cookies",
                       home / "vesper-voices" / "pack" / "jarvis.safetensors", workspace / "notes.txt",
                       workspace / "cookies.db"):
            resp = await client.get(f"/vesper/files?path={target}", headers=VOUCHED)
            assert resp.status == 403, target
        assert (await client.get("/vesper/files?path=relative.txt", headers=VOUCHED)).status == 400
