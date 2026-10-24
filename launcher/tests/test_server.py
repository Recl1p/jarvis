"""The launcher's web server: routing before and after setup, the request guards, pairing and the proxy."""

import aiohttp
import pytest

from jarvis_launcher import auth, config, connect, server, state

from fake_hermes import KEY, make_app

H = {"X-JARVIS": "1"}


PASSWORD = "correct horse battery"


@pytest.fixture
async def client(aiohttp_client):
    """A client that sends JARVIS's password as Basic auth, so these tests are about what lies behind the login."""
    auth.set_password(PASSWORD)
    # A default header rather than auth=, so a test can still send its own Authorization (the Bearer key) instead.
    return await aiohttp_client(server.build_app(),
                                headers={"Authorization": aiohttp.BasicAuth("jarvis", PASSWORD).encode()})


async def connect_to(aiohttp_server, **kwargs):
    hermes = await aiohttp_server(make_app(**kwargs))
    state.update(mode="external", hermes_url=str(hermes.make_url("")).rstrip("/"), api_key=KEY)
    return hermes


async def test_before_setup_the_setup_screen_answers(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/jarvis/"
    resp = await client.get("/vesper", allow_redirects=False)
    assert resp.headers["Location"] == "/jarvis/"
    page = await (await client.get("/jarvis/")).text()
    assert 'data-jarvis-key=""' in page
    assert (await client.get("/v1/models")).status == 503
    assert (await (await client.get("/health")).json())["platform"] == "jarvis-launcher"
    data = await (await client.get("/jarvis/api/state")).json()
    assert data["configured"] is False and data["mode"] is None


async def test_static_files_cannot_escape_their_folder(client):
    assert (await client.get("/jarvis/static/setup.js")).status == 200
    assert (await client.get("/jarvis/static/..%2Fserver.py")).status == 404


async def test_changes_need_the_header_the_origin_and_consent(client):
    assert (await client.post("/jarvis/api/install", json={"accept": True})).status == 403
    resp = await client.post("/jarvis/api/install", json={"accept": True}, headers={**H, "Origin": "http://evil.example"})
    assert resp.status == 403
    resp = await client.post("/jarvis/api/install", json={"accept": False}, headers=H)
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "consent_required"


async def test_once_set_up_changes_need_the_key(client, aiohttp_server):
    await connect_to(aiohttp_server)
    assert (await client.post("/jarvis/api/disconnect", headers=H)).status == 401
    resp = await client.post("/jarvis/api/disconnect", headers={**H, "Authorization": f"Bearer {KEY}"})
    assert resp.status == 200
    assert state.load()["mode"] is None


async def test_setup_page_carries_the_key_once_connected(client, aiohttp_server):
    await connect_to(aiohttp_server)
    page = await (await client.get("/jarvis/")).text()
    assert f'data-jarvis-key="{KEY}"' in page


async def test_proxy_keeps_host_streams_and_large_bodies(client, aiohttp_server):
    await connect_to(aiohttp_server)
    resp = await client.post("/echo/x?y=1", data=b"z" * (12 * 1024 * 1024), headers={"Host": "jarvis.example:8642"})
    body = await resp.json()
    assert body["host"] == "jarvis.example:8642"
    assert body["path"] == "/echo/x?y=1" and body["length"] == 12 * 1024 * 1024
    resp = await client.get("/stream")
    assert (await resp.text()) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert (await client.get("/vesper")).status == 200


async def test_proxy_reports_an_unreachable_hermes(client):
    state.update(mode="external", hermes_url="http://127.0.0.1:9", api_key=KEY)
    resp = await client.get("/vesper")
    assert resp.status == 502 and (await resp.json())["error"]["code"] == "hermes_unreachable"


async def test_connect_answers_with_the_checks_and_saves_only_a_working_hermes(client, aiohttp_server):
    bad = await aiohttp_server(make_app(connector=None))
    resp = await client.post("/jarvis/api/connect", json={"url": str(bad.make_url("")), "api_key": KEY}, headers=H)
    data = await resp.json()
    assert resp.status == 200 and data["connected"] is False and data["checks"]
    assert state.load()["mode"] is None
    from jarvis_launcher import hermes_setup
    good = await aiohttp_server(make_app(connector=hermes_setup.bundled_connector_version()))
    resp = await client.post("/jarvis/api/connect", json={"url": str(good.make_url("")) + "/vesper", "api_key": KEY}, headers=H)
    data = await resp.json()
    assert data["connected"] is True
    assert state.load()["mode"] == "external" and state.load()["hermes_url"] == str(good.make_url("")).rstrip("/")


async def test_pairing_connects_by_itself(client, aiohttp_server, monkeypatch):
    from jarvis_launcher import hermes_setup
    hermes = await aiohttp_server(make_app(connector=hermes_setup.bundled_connector_version()))
    prompt = await (await client.post("/jarvis/api/connect-prompt", json={}, headers=H)).json()
    assert prompt["code"] in prompt["prompt"] and "/jarvis/connect.sh" in prompt["command"]
    resp = await client.post("/jarvis/api/pair", headers=H, json={
        "code": prompt["code"], "urls": ["http://127.0.0.1:9", str(hermes.make_url(""))], "api_key": KEY})
    assert resp.status == 202
    import asyncio
    for _ in range(40):
        if state.load()["mode"] == "external":
            break
        await asyncio.sleep(0.25)
    assert state.load()["hermes_url"] == str(hermes.make_url("")).rstrip("/")
    # The code worked once.
    resp = await client.post("/jarvis/api/pair", headers=H, json={"code": prompt["code"], "urls": ["http://x"], "api_key": KEY})
    assert resp.status == 403


async def test_update_settings_are_validated(client, aiohttp_server):
    await connect_to(aiohttp_server)
    auth = {**H, "Authorization": f"Bearer {KEY}"}
    resp = await client.put("/jarvis/api/update/settings", json={"auto": False, "hour": 99}, headers=auth)
    assert (await resp.json()) == {"auto": False, "hour": 23}
    assert (await client.put("/jarvis/api/update/settings", json={"hour": "noon"}, headers=auth)).status == 400
    # Updating is for a Hermes JARVIS installed.
    assert (await client.post("/jarvis/api/update/run", json={}, headers=auth)).status == 409


async def test_connect_script_and_connector_are_served(client):
    script = await (await client.get("/jarvis/connect.sh")).text()
    assert script.startswith("#!/usr/bin/env bash") and "@JARVIS_URL@" not in script
    resp = await client.get("/jarvis/connector.tar.gz")
    assert resp.status == 200 and (await resp.read())[:2] == b"\x1f\x8b"


def test_clean_url():
    assert server._clean_url("192.168.1.5:8642") == "http://192.168.1.5:8642"
    assert server._clean_url("http://h:8642/vesper") == "http://h:8642"
    assert server._clean_url(" http://h:8642/ ") == "http://h:8642"
    assert server._clean_url("") == ""
