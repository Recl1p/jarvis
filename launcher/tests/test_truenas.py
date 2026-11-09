"""Hermes as a TrueNAS app: the TrueNAS API client, the app update with its health check and rollback, the schedule's
rules, the DevTools proxy and the shared-data-folder guards. TrueNAS is a stand-in JSON-RPC server (mock_truenas)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp import web

from jarvis_launcher import cdp, config, server, state, truenas, updates
from jarvis_launcher.managed import Gateway, InstallJob

from fake_hermes import KEY, make_app

GOOD_KEY = "1-truenas-key"


def mock_truenas(*, version="1.0.37", latest="1.0.38", fail_upgrade=False):
    """A TrueNAS API with one app, "hermes". Records every call in app["calls"]."""
    apps = {"hermes": {"name": "hermes", "state": "RUNNING", "version": version, "human_version": f"v0.22.5_{version}",
                       "upgrade_available": version != latest, "latest_version": latest,
                       "latest_app_version": "v0.23.0", "metadata": {"app_version": "v0.22.5"}}}
    jobs = {}
    calls = []

    async def rpc(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logged_in = False
        async for message in ws:
            req = json.loads(message.data)
            method, params, rid = req["method"], req.get("params") or [], req["id"]
            calls.append((method, params))
            await ws.send_str(json.dumps({"msg": "notify", "collection": "noise"}))   # events come in between
            if method == "auth.login_with_api_key":
                logged_in = params[0] == GOOD_KEY
                result = logged_in
            elif not logged_in:
                await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid,
                                              "error": {"code": 13, "message": "Not authenticated"}}))
                continue
            elif method == "app.query":
                name = params[0][0][2]
                result = [dict(apps[name])] if name in apps else []
            elif method in ("app.upgrade", "app.rollback", "app.redeploy"):
                jid = len(jobs) + 1
                jobs[jid] = {"method": method, "params": params, "polls": 0}
                result = jid
            elif method == "core.get_jobs":
                job = jobs[params[0][0][2]]
                job["polls"] += 1
                if job["polls"] < 2:
                    result = [{"id": params[0][0][2], "state": "RUNNING", "progress": {"description": "Pulling images"}}]
                elif job["method"] == "app.upgrade" and fail_upgrade:
                    result = [{"id": params[0][0][2], "state": "FAILED", "error": "image pull failed"}]
                else:
                    app = apps[job["params"][0]]
                    if job["method"] == "app.upgrade":
                        app.update(version=app["latest_version"], upgrade_available=False)
                    elif job["method"] == "app.rollback":
                        app.update(version=job["params"][1]["app_version"], upgrade_available=True)
                    result = [{"id": params[0][0][2], "state": "SUCCESS", "result": True,
                               "progress": {"description": "Done"}}]
            else:
                result = None
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))
        return ws

    app = web.Application()
    app.router.add_get("/api/current", rpc)
    app["calls"], app["apps"] = calls, apps
    return app


@pytest.fixture
async def nas(aiohttp_server, monkeypatch):
    async def start(**kwargs):
        srv = await aiohttp_server(mock_truenas(**kwargs))
        monkeypatch.setattr(truenas, "_secure", lambda url: True)        # the stand-in speaks plain ws://
        monkeypatch.setattr(truenas, "JOB_POLL_SECONDS", 0.01)
        monkeypatch.setattr(config, "TRUENAS_URL", str(srv.make_url("/api/current")).replace("http://", "ws://"))
        monkeypatch.setattr(config, "TRUENAS_API_KEY", GOOD_KEY)
        monkeypatch.setattr(config, "TRUENAS_APP", "hermes")
        return srv.app
    return start


async def connected_hermes(aiohttp_server, **kwargs):
    hermes = await aiohttp_server(make_app(**kwargs))
    state.update(mode="external", hermes_url=str(hermes.make_url("")).rstrip("/"), api_key=KEY)
    return hermes


def updater():
    gateway = Gateway()
    return updates.Updater(gateway, InstallJob(gateway))


# --- the client ------------------------------------------------------------------------------------------------------

async def test_the_key_never_goes_over_plain_http(monkeypatch):
    monkeypatch.setattr(config, "TRUENAS_API_KEY", GOOD_KEY)
    for url in ("ws://nas/api/current", "http://nas/api/current", "https://nas/api/current"):
        with pytest.raises(truenas.TrueNASError, match="wss://"):
            async with truenas.Client(url=url):
                pass


async def test_a_wrong_key_is_an_error(nas, monkeypatch):
    await nas()
    monkeypatch.setattr(config, "TRUENAS_API_KEY", "wrong")
    with pytest.raises(truenas.TrueNASError, match="did not accept"):
        await truenas.app_info()


async def test_cert_pin_must_be_a_sha256(monkeypatch):
    monkeypatch.setattr(config, "TRUENAS_CERT_SHA256", "ab:cd")
    with pytest.raises(truenas.TrueNASError, match="SHA-256"):
        truenas._ssl()
    monkeypatch.setattr(config, "TRUENAS_CERT_SHA256", ":".join(["ab"] * 32))
    assert isinstance(truenas._ssl(), aiohttp.Fingerprint)


async def test_app_info_skips_events_and_reads_the_app(nas):
    await nas()
    info = await truenas.app_info()
    assert info["version"] == "1.0.37" and info["upgrade_available"] and info["latest_version"] == "1.0.38"
    assert info["app_version"] == "v0.22.5"


async def test_an_unknown_app_is_an_error(nas, monkeypatch):
    await nas()
    monkeypatch.setattr(config, "TRUENAS_APP", "nope")
    with pytest.raises(truenas.TrueNASError, match="no app named"):
        await truenas.app_info()


# --- checking and updating -------------------------------------------------------------------------------------------

async def test_kind_needs_external_mode_and_the_truenas_settings(nas):
    assert updates.kind({"mode": "managed"}) == "managed"
    assert updates.kind({"mode": "external"}) is None
    await nas()
    assert updates.kind({"mode": "external"}) == "truenas"
    assert updates.kind({"mode": None}) is None


async def test_check_reports_the_app_upgrade(nas, aiohttp_server):
    await nas()
    await connected_hermes(aiohttp_server, version="0.22.5")
    result = await updater().check()
    assert result["available"] and result["target"] == "1.0.38" and result["current"] == "0.22.5"
    assert "1.0.38" in result["output"]
    assert state.load()["updates"]["last_check_result"]["target"] == "1.0.38"


async def test_update_upgrades_the_app_and_records_it(nas, aiohttp_server):
    truenas_app = await nas()
    await connected_hermes(aiohttp_server, connector="2.2.0")
    u = updater()
    assert u.start_update("requested")
    await u._task
    last = state.load()["updates"]["last_result"]
    assert last["ok"] is True and last["from_app"] == "1.0.37" and last["to_app"] == "1.0.38", last
    upgrade = next(p for m, p in truenas_app["calls"] if m == "app.upgrade")
    assert upgrade == ["hermes", {"app_version": "latest", "snapshot_hostpaths": True}]
    assert not any(m == "app.rollback" for m, _ in truenas_app["calls"])
    assert "Pulling images" in "\n".join(u.log.since(0)[0])
    assert u.busy is False


async def test_an_unhealthy_upgrade_is_rolled_back(nas, aiohttp_server, monkeypatch):
    truenas_app = await nas()
    await connected_hermes(aiohttp_server, connector=None)       # the connector never answers after the upgrade
    monkeypatch.setattr(updates, "wait_healthy", _quick_wait_healthy)
    u = updater()
    u.start_update("requested")
    await u._task
    last = state.load()["updates"]["last_result"]
    assert last["ok"] is False and last["rollback"].startswith("failed"), last   # still unhealthy after rolling back
    rollback = next(p for m, p in truenas_app["calls"] if m == "app.rollback")
    assert rollback == ["hermes", {"app_version": "1.0.37", "rollback_snapshot": True}]
    assert truenas_app["apps"]["hermes"]["version"] == "1.0.37"


async def test_a_failed_upgrade_job_needs_no_rollback(nas, aiohttp_server):
    truenas_app = await nas(fail_upgrade=True)
    await connected_hermes(aiohttp_server, connector="2.2.0")
    u = updater()
    u.start_update("requested")
    await u._task
    last = state.load()["updates"]["last_result"]
    assert last["ok"] is False and "image pull failed" in last["problem"]
    assert last["rollback"].startswith("not needed")
    assert not any(m == "app.rollback" for m, _ in truenas_app["calls"])


async def test_nothing_to_upgrade_is_not_a_failure(nas, aiohttp_server):
    truenas_app = await nas(version="1.0.38")
    await connected_hermes(aiohttp_server, connector="2.2.0")
    u = updater()
    u.start_update("scheduled")
    await u._task
    assert state.load()["updates"]["last_result"]["ok"] is True
    assert not any(m == "app.upgrade" for m, _ in truenas_app["calls"])


async def _quick_wait_healthy(url, key, timeout=0):
    return "the JARVIS connector did not load (HTTP 404)"


# --- the schedule ----------------------------------------------------------------------------------------------------

def test_checked_today_compares_local_dates():
    local = datetime(2026, 11, 9, 4, 5).astimezone()
    earlier_today_utc = local.replace(hour=0, minute=1).astimezone(timezone.utc).isoformat()
    yesterday_utc = (local - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    assert updates.Updater._checked_today(earlier_today_utc, local)
    assert not updates.Updater._checked_today(yesterday_utc, local)
    assert not updates.Updater._checked_today("", local)


def test_a_release_that_failed_is_not_retried_by_the_schedule():
    failed = {"last_result": {"ok": False, "target": "stable v0.23.0 (2bd378ed1b8d)", "problem": "x", "at": "t"}}
    assert updates.Updater._failed_before(failed, "stable v0.23.0 (2bd378ed1b8d)")
    assert not updates.Updater._failed_before(failed, "stable v0.23.1 (aaaa)")
    assert not updates.Updater._failed_before({"last_result": {"ok": True, "target": "v"}}, "v")
    assert not updates.Updater._failed_before(failed, "")


# --- the setup API ---------------------------------------------------------------------------------------------------

PASSWORD = "correct horse battery"


@pytest.fixture
async def client(aiohttp_client):
    from jarvis_launcher import auth
    auth.set_password(PASSWORD)
    return await aiohttp_client(server.build_app(),
                                headers={"Authorization": aiohttp.BasicAuth("jarvis", PASSWORD).encode()})


async def test_the_updates_card_works_for_the_truenas_app(client, nas, aiohttp_server):
    await nas()
    await connected_hermes(aiohttp_server, version="0.22.5")
    headers = {"X-JARVIS": "1", "Authorization": f"Bearer {KEY}"}
    data = await (await client.get("/jarvis/api/state")).json()
    assert data["updates"]["kind"] == "truenas" and data["updates"]["truenas_app"] == "hermes"
    resp = await client.post("/jarvis/api/update/check", headers=headers)
    assert resp.status == 200 and (await resp.json())["available"] is True


async def test_an_external_hermes_without_truenas_is_not_updated(client, aiohttp_server):
    await connected_hermes(aiohttp_server)
    headers = {"X-JARVIS": "1", "Authorization": f"Bearer {KEY}"}
    assert (await client.post("/jarvis/api/update/run", headers=headers)).status == 409


async def test_a_shared_data_folder_never_gets_a_second_hermes(client, aiohttp_server, monkeypatch):
    monkeypatch.setattr(config, "SHARED_HOME", True)
    await connected_hermes(aiohttp_server)
    headers = {"X-JARVIS": "1", "Authorization": f"Bearer {KEY}"}
    for path, body in (("/jarvis/api/use-managed", {}), ("/jarvis/api/install", {"accept": True})):
        resp = await client.post(path, json=body, headers=headers)
        if resp.status == 409 and (await resp.json())["error"]["code"] == "external":
            continue      # install refuses an external Hermes first; either refusal keeps the folder to one Hermes
        assert resp.status == 409 and (await resp.json())["error"]["code"] == "shared_home", path
    data = await (await client.get("/jarvis/api/state")).json()
    assert data["hermes"]["shared_home"] is True


# --- the DevTools proxy ----------------------------------------------------------------------------------------------

def fake_chromium():
    async def version(request):
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return web.Response(status=500, text="Host header is specified and is not an IP address or localhost.")
        return web.json_response({"Browser": "Chrome/156",
                                  "webSocketDebuggerUrl": f"ws://{request.host}/devtools/browser/abc"})

    async def devtools(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            await ws.send_str(json.dumps({"echo": json.loads(message.data), "origin": request.headers.get("Origin")}))
        return ws

    app = web.Application()
    app.router.add_get("/json/version", version)
    app.router.add_get("/devtools/browser/abc", devtools)
    return app


async def test_the_devtools_proxy_rewrites_host_and_addresses(aiohttp_server, aiohttp_client):
    chromium = await aiohttp_server(fake_chromium())
    proxy = cdp.CDPProxy(str(chromium.make_url("")).rstrip("/"))
    client = await aiohttp_client(proxy.app())
    resp = await client.get("/json/version", headers={"Host": "jarvis:9223"})
    assert resp.status == 200
    data = await resp.json()
    assert data["webSocketDebuggerUrl"] == "ws://jarvis:9223/devtools/browser/abc"
    ws = await client.ws_connect("/devtools/browser/abc", headers={"Origin": "http://evil.example"})
    await ws.send_str(json.dumps({"id": 1, "method": "Browser.getVersion"}))
    answer = json.loads((await ws.receive()).data)
    assert answer["echo"]["id"] == 1 and answer["origin"] is None       # the client's Origin is not passed on
    await ws.close()
    await proxy.close()


def test_listen_addresses():
    assert cdp._listen("0.0.0.0:9223") == ("0.0.0.0", 9223)
    assert cdp._listen(":9223") == ("0.0.0.0", 9223)
    assert cdp._listen("[::]:9223") == ("::", 9223)
