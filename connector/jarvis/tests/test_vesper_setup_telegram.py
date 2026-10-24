"""The setup guide's Telegram step: a BotFather token checked, the user found from their Start message, the bot saved
the way `hermes gateway setup` saves it, and handed to the running gateway without a restart. Telegram's Bot API is
a local fake server; HERMES_HOME is a temp dir; config loading is Hermes' real code."""

import asyncio
import time

import pytest
import pytest_asyncio
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from jarvis import setup_telegram as setup
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
TOKEN = "123456789:" + "AAEb" * 9
OLD_TOKEN = "987654321:" + "ZZqq" * 9
ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL")


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ENV_VARS:   # save_env_value publishes into os.environ: register each so teardown removes it
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)
    monkeypatch.setattr(setup, "_pending", {})
    monkeypatch.setattr(setup, "_bot_cache", {})
    return tmp_path


def make_app(runner=None):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    adapter.gateway_runner = runner
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def message(update_id, user_id, first, chat_type="private", is_bot=False, username="abd"):
    return {"update_id": update_id, "message": {"message_id": update_id, "text": "/start",
            "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot, "first_name": first, "username": username}}}


@pytest_asyncio.fixture
async def telegram(monkeypatch):
    """The Bot API for TOKEN (and OLD_TOKEN): records every call."""
    state = {"calls": [], "updates": [message(41, 5550001, "Abd"), message(42, 5550002, "Group", chat_type="group"),
                                      message(43, 5550003, "Other bot", is_bot=True)]}

    async def method(request):
        token, name = request.match_info["token"], request.match_info["method"]
        params = await request.json() if request.can_read_body else {}
        state["calls"].append((token, name, params))
        if token not in (TOKEN, OLD_TOKEN):
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        if name == "getMe":
            return web.json_response({"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "My JARVIS",
                                                             "username": "my_jarvis_bot" if token == TOKEN else "old_bot"}})
        if name == "getUpdates":
            offset = params.get("offset")
            return web.json_response({"ok": True, "result": [u for u in state["updates"] if not offset or u["update_id"] >= offset]})
        if name == "sendMessage":
            return web.json_response({"ok": True, "result": {"message_id": 99}})
        return web.json_response({"ok": False, "description": "Not Found"}, status=404)

    application = web.Application()
    application.router.add_post("/bot{token}/{method}", method)
    async with TestServer(application, host="127.0.0.1") as server:
        monkeypatch.setattr(setup, "TELEGRAM_API", str(server.make_url("")).rstrip("/"))
        yield state


def env_file(home):
    path = home / ".env"
    return path.read_text() if path.exists() else ""


async def check_and_find(client):
    checked = await (await client.post("/vesper/setup/telegram/check", headers=AUTH, json={"token": TOKEN})).json()
    found = await (await client.post("/vesper/setup/telegram/find", headers=AUTH, json={"setup_id": checked["setup_id"]})).json()
    return checked, found


class OldTelegram:
    platform = Platform.TELEGRAM

    def __init__(self):
        self.disconnected = False

    async def cancel_background_tasks(self):
        return None

    async def disconnect(self):
        self.disconnected = True


def running_gateway(old_adapter=None):
    """The real GatewayRunner methods the guide calls, on a runner with just the state they read."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.config = GatewayConfig(platforms={})
    runner.adapters = {Platform.TELEGRAM: old_adapter} if old_adapter else {}
    runner._failed_platforms = {}
    runner._reconnect_watcher_task = asyncio.get_running_loop().create_future()   # alive: nothing to respawn
    return runner


@pytest.mark.asyncio
async def test_every_telegram_route_needs_the_dashboard_key(telegram):
    async with TestClient(TestServer(make_app())) as client:
        for method, path in (("GET", "/vesper/setup/telegram"), ("POST", "/vesper/setup/telegram/check"),
                             ("POST", "/vesper/setup/telegram/find"), ("POST", "/vesper/setup/telegram/connect")):
            assert (await client.request(method, path, json={})).status == 401


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused_before_telegram_and_by_telegram(telegram):
    async with TestClient(TestServer(make_app())) as client:
        resp = await client.post("/vesper/setup/telegram/check", headers=AUTH, json={"token": "not a token"})
        assert (await resp.json())["error"]["code"] == "invalid_token" and telegram["calls"] == []
        resp = await client.post("/vesper/setup/telegram/check", headers=AUTH, json={"token": "111:" + "x" * 35})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_the_bot_is_named_and_only_people_writing_privately_are_offered(telegram):
    async with TestClient(TestServer(make_app())) as client:
        checked, found = await check_and_find(client)
    assert checked["bot"] == {"username": "my_jarvis_bot", "name": "My JARVIS", "link": "https://t.me/my_jarvis_bot"}
    assert TOKEN not in str(checked) and checked["already_connected"] is False
    assert found["senders"] == [{"id": "5550001", "name": "Abd", "username": "abd"}]


@pytest.mark.asyncio
async def test_connecting_saves_what_gateway_setup_saves_and_quiets_the_setup_messages(telegram, hermes_home):
    async with TestClient(TestServer(make_app())) as client:
        checked, _found = await check_and_find(client)
        resp = await client.post("/vesper/setup/telegram/connect", headers=AUTH,
                                 json={"setup_id": checked["setup_id"], "user_id": "5550003"})
        assert resp.status == 400   # not someone who wrote to it privately
        resp = await client.post("/vesper/setup/telegram/connect", headers=AUTH,
                                 json={"setup_id": checked["setup_id"], "user_id": "5550001", "persona": "cortana"})
        body = await resp.json()
        again = await client.post("/vesper/setup/telegram/connect", headers=AUTH,
                                  json={"setup_id": checked["setup_id"], "user_id": "5550001"})
    assert body["ok"] is True and body["started"] is False and body["restart_needed"] is True   # no gateway here
    assert again.status == 404   # a setup is used once
    env = env_file(hermes_home)
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}" in env and "TELEGRAM_ALLOWED_USERS=5550001" in env
    assert "TELEGRAM_HOME_CHANNEL=5550001" in env
    assert yaml.safe_load((hermes_home / "config.yaml").read_text())["platforms"]["telegram"]["enabled"] is True
    from gateway.config import load_gateway_config
    pconf = load_gateway_config().platforms[Platform.TELEGRAM]
    assert pconf.enabled and pconf.token == TOKEN and pconf.home_channel.chat_id == "5550001"
    calls = [(name, params) for _t, name, params in telegram["calls"]]
    assert ("getUpdates", {"offset": 44, "timeout": 0}) in calls   # marks the Start messages read
    assert ("sendMessage", {"chat_id": 5550001, "text": "✓ Cortana is connected. Message me here any time."}) in calls


@pytest.mark.asyncio
async def test_a_new_bot_replaces_the_running_one_in_the_gateway_without_a_restart(telegram, hermes_home, monkeypatch):
    from gateway.run import _platform_has_bot_credential
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", OLD_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1234")
    old = OldTelegram()
    runner = running_gateway(old)
    async with TestClient(TestServer(make_app(runner))) as client:
        before = await (await client.get("/vesper/setup/telegram", headers=AUTH)).json()
        checked, _found = await check_and_find(client)
        body = await (await client.post("/vesper/setup/telegram/connect", headers=AUTH,
                                        json={"setup_id": checked["setup_id"], "user_id": "5550001"})).json()
        after = await (await client.get("/vesper/setup/telegram", headers=AUTH)).json()
    assert before["running"] is True and before["bot"]["username"] == "old_bot" and before["allowed"] == 1
    assert body["started"] is True and old.disconnected and Platform.TELEGRAM not in runner.adapters
    queued = runner._failed_platforms[Platform.TELEGRAM]
    assert queued["config"].token == TOKEN and queued["next_retry"] <= time.monotonic()
    assert _platform_has_bot_credential(Platform.TELEGRAM, queued["config"])   # the watcher's gate lets it through
    assert runner.config.platforms[Platform.TELEGRAM].token == TOKEN
    assert "TELEGRAM_ALLOWED_USERS=5550001" in env_file(hermes_home)   # a new bot: only the person who set it up
    assert after["configured"] is True and after["running"] is False and after["bot"]["username"] == "my_jarvis_bot"


@pytest.mark.asyncio
async def test_the_bot_the_gateway_is_reading_is_never_polled_by_the_guide(telegram, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    runner = running_gateway(OldTelegram())
    async with TestClient(TestServer(make_app(runner))) as client:
        checked = await (await client.post("/vesper/setup/telegram/check", headers=AUTH, json={"token": TOKEN})).json()
        resp = await client.post("/vesper/setup/telegram/find", headers=AUTH, json={"setup_id": checked["setup_id"]})
        refused = await resp.json()
    assert checked["already_connected"] is True
    assert resp.status == 409 and refused["error"]["code"] == "already_connected"
    assert not any(name == "getUpdates" for _t, name, _p in telegram["calls"])
