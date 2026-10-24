"""The login in front of everything: creating the password, logging in and out, Basic auth and the API key, the paths
that stay open, guessing limits, and the browser desktop behind it."""

import os

import aiohttp
import pytest
from aiohttp import web

from jarvis_launcher import auth, config, server, state

from fake_hermes import KEY, make_app

H = {"X-JARVIS": "1"}
PAGE = {"Accept": "text/html,application/xhtml+xml"}
PASSWORD = "correct horse battery"


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(server.build_app())


def basic(password=PASSWORD):
    return {"Authorization": aiohttp.BasicAuth("jarvis", password).encode()}


async def connect_to(aiohttp_server):
    hermes = await aiohttp_server(make_app())
    state.update(mode="external", hermes_url=str(hermes.make_url("")).rstrip("/"), api_key=KEY)
    return hermes


async def test_first_visit_asks_for_a_password_and_nothing_else_is_open(client):
    resp = await client.get("/", headers=PAGE, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/jarvis/login?next=/"
    resp = await client.get("/vesper", headers=PAGE, allow_redirects=False)
    assert resp.headers["Location"] == "/jarvis/login?next=/vesper"
    resp = await client.get("/jarvis/api/state")
    assert resp.status == 401 and (await resp.json())["error"]["code"] == "password_not_set"
    assert (await client.post("/jarvis/api/install", json={"accept": True}, headers=H)).status == 401
    assert (await client.get("/desktop/vnc.html")).status == 401
    # Open: health, the login page and what it loads, and what someone's own Hermes fetches to connect.
    for path in ("/jarvis/health", "/health", "/jarvis/login", "/jarvis/static/login.js", "/jarvis/static/setup.css",
                 "/jarvis/api/auth", "/jarvis/connect.sh", "/jarvis/connector.tar.gz"):
        assert (await client.get(path)).status == 200, path
    status = await (await client.get("/jarvis/api/auth")).json()
    assert status == {"enabled": True, "has_password": False, "from_env": False, "min_length": 8, "logged_in": False}


async def test_creating_the_password_logs_in_and_only_works_once(client):
    resp = await client.post("/jarvis/api/password", json={"password": "short"}, headers=H)
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "weak_password"
    assert (await client.post("/jarvis/api/password", json={"password": PASSWORD})).status == 403   # no X-JARVIS
    resp = await client.post("/jarvis/api/password", json={"password": PASSWORD}, headers=H)
    assert resp.status == 200 and auth.COOKIE in resp.cookies
    cookie = resp.cookies[auth.COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert (await client.get("/jarvis/api/state")).status == 200          # the client keeps the cookie
    client.session.cookie_jar.clear()
    resp = await client.post("/jarvis/api/password", json={"password": "someone else's"}, headers=H)
    assert resp.status == 401
    assert auth.check_password(PASSWORD) and not auth.check_password("someone else's")


async def test_login_logout_and_where_it_goes_next(client):
    auth.set_password(PASSWORD)
    resp = await client.post("/jarvis/api/login", json={"password": "wrong password"}, headers=H)
    assert resp.status == 401 and (await resp.json())["error"]["code"] == "wrong_password"
    resp = await client.post("/jarvis/api/login", json={"password": PASSWORD, "next": "//evil.example/x"}, headers=H)
    assert resp.status == 200 and (await resp.json())["next"] == "/jarvis/"
    assert (await client.get("/jarvis/api/state")).status == 200
    resp = await client.get("/jarvis/login?next=/vesper", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/vesper"
    for evil in ("https://evil.example", "//evil.example", "/%09/evil.example", "/%0a/evil.example", "/%5Cevil.example"):
        resp = await client.get("/jarvis/login?next=" + evil, allow_redirects=False)
        assert resp.headers["Location"] == "/jarvis/", evil
    assert server._safe_next("/vesper?x=1") == "/vesper?x=1" and server._safe_next("/\t/evil") == "/jarvis/"
    token = client.session.cookie_jar.filter_cookies(client.make_url("/"))[auth.COOKIE].value
    assert (await client.post("/jarvis/api/logout", json={}, headers=H)).status == 200
    assert not auth.valid_session(token)
    assert (await client.get("/jarvis/api/state")).status == 401


async def test_wrong_passwords_lock_the_address_out(client):
    auth.set_password(PASSWORD)
    for _ in range(auth.FREE_FAILURES):
        assert (await client.post("/jarvis/api/login", json={"password": "nope nope"}, headers=H)).status == 401
    resp = await client.post("/jarvis/api/login", json={"password": PASSWORD}, headers=H)
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    # Basic auth counts against the same limit.
    assert (await client.get("/jarvis/api/state", headers=basic())).status == 401


async def test_basic_auth_and_the_api_key_pass(client, aiohttp_server):
    auth.set_password(PASSWORD)
    assert (await client.get("/jarvis/api/state", headers=basic())).status == 200
    assert (await client.get("/jarvis/api/state", headers=basic("not it at all"))).status == 401
    resp = await client.get("/jarvis/api/state", headers={"Authorization": "Bearer " + KEY})
    assert resp.status == 401                                             # no Hermes yet, so no key
    await connect_to(aiohttp_server)
    assert (await client.get("/jarvis/api/state", headers={"Authorization": "Bearer " + KEY})).status == 200
    assert (await client.get("/v1/models", headers={"Authorization": "Bearer " + KEY})).status == 200
    assert (await client.get("/v1/models", headers={"Authorization": "Bearer " + "x" * 64})).status == 401


async def test_dotted_and_encoded_paths_are_not_public(client):
    auth.set_password(PASSWORD)
    for path in ("/jarvis/static/%2e%2e/api/state", "/vesper/icons/%2E%2E/%2E%2E/v1/models",
                 "/jarvis/static//etc/passwd", "/jarvis/health/../api/state"):
        resp = await client.get(path)
        assert resp.status in (401, 404), path
        assert resp.status != 200


async def test_changing_the_password_needs_the_current_one_and_ends_other_logins(client, aiohttp_client):
    auth.set_password(PASSWORD)
    other = await aiohttp_client(server.build_app())
    assert (await other.post("/jarvis/api/login", json={"password": PASSWORD}, headers=H)).status == 200
    await client.post("/jarvis/api/login", json={"password": PASSWORD}, headers=H)
    resp = await client.post("/jarvis/api/password", json={"current": "wrong one", "password": "new password 123"},
                             headers=H)
    assert resp.status == 401
    resp = await client.post("/jarvis/api/password", json={"current": PASSWORD, "password": "new password 123"},
                             headers=H)
    assert resp.status == 200
    assert (await client.get("/jarvis/api/state")).status == 200           # this browser got a new session
    assert (await other.get("/jarvis/api/state")).status == 401            # the other one is logged out
    assert auth.check_password("new password 123")


async def test_proxy_never_passes_the_login_on(client, aiohttp_server):
    auth.set_password(PASSWORD)
    await connect_to(aiohttp_server)
    echoed = await (await client.get("/echo/x", headers=basic())).json()
    assert echoed["authorization"] == "" and echoed["cookie"] == ""
    await client.post("/jarvis/api/login", json={"password": PASSWORD}, headers=H)
    echoed = await (await client.get("/echo/x", headers={"Cookie": "theme=dark"})).json()
    assert auth.COOKIE not in echoed["cookie"] and "theme=dark" in echoed["cookie"]
    echoed = await (await client.get("/echo/x", headers={"Authorization": "Bearer " + KEY})).json()
    assert echoed["authorization"] == "Bearer " + KEY
    # Past the login, the launcher vouches for the request; a browser can't forge that itself.
    echoed = await (await client.get("/echo/x", headers={"X-JARVIS-Launcher": "forged"})).json()
    assert echoed["launcher"] == KEY


async def test_the_desktop_is_behind_the_login(client, aiohttp_server, monkeypatch):
    async def page(request):
        return web.Response(text="noVNC " + request.path_qs, content_type="text/html")

    async def socket(request):
        ws = web.WebSocketResponse(protocols=("binary",))
        await ws.prepare(request)
        async for msg in ws:
            await ws.send_bytes(b"echo:" + msg.data)
        return ws

    desktop = web.Application()
    desktop.router.add_get("/websockify", socket)
    desktop.router.add_get("/{tail:.*}", page)
    upstream = await aiohttp_server(desktop)
    monkeypatch.setattr(config, "DESKTOP_URL", str(upstream.make_url("")).rstrip("/"))
    auth.set_password(PASSWORD)
    assert (await client.get("/desktop/vnc.html")).status == 401
    resp = await client.get("/desktop/", headers=basic(), allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == server.DESKTOP_PAGE
    resp = await client.get("/desktop/vnc.html?autoconnect=true", headers=basic())
    assert resp.status == 200 and (await resp.text()) == "noVNC /vnc.html?autoconnect=true"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    async with client.ws_connect("/desktop/websockify", headers=basic(), protocols=("binary",)) as ws:
        assert ws.protocol == "binary"
        await ws.send_bytes(b"\x01\x02")
        assert (await ws.receive()).data == b"echo:\x01\x02"
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect("/desktop/websockify", protocols=("binary",))


async def test_desktop_down_is_reported(client, monkeypatch):
    monkeypatch.setattr(config, "DESKTOP_URL", "http://127.0.0.1:9")
    auth.set_password(PASSWORD)
    resp = await client.get("/desktop/vnc.html", headers=basic())
    assert resp.status == 502 and "browser desktop" in (await resp.json())["error"]["message"]


async def test_login_can_be_turned_off_or_set_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH", "off")
    assert (await client.get("/jarvis/api/state")).status == 200
    monkeypatch.setenv("JARVIS_AUTH", "on")
    monkeypatch.setenv("JARVIS_PASSWORD", "from the compose file")
    assert (await client.get("/jarvis/api/state")).status == 401
    assert (await client.get("/jarvis/api/state", headers=basic("from the compose file"))).status == 200
    resp = await client.post("/jarvis/api/password", json={"password": "something new"}, headers=H)
    assert resp.status == 409 and (await resp.json())["error"]["code"] == "from_env"
    assert (await client.post("/jarvis/api/login", json={"password": "from the compose file"}, headers=H)).status == 200


def test_the_password_file_holds_no_password_or_token():
    auth.set_password(PASSWORD)
    token = auth.new_session()
    text = (config.STATE_DIR / "auth.json").read_text()
    assert PASSWORD not in text and token not in text
    assert oct(os.stat(config.STATE_DIR / "auth.json").st_mode & 0o777) == "0o600"
    assert auth.valid_session(token) and not auth.valid_session(token + "x") and not auth.valid_session("")
