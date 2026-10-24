"""Free OpenRouter setup for the dashboard: GET /vesper/setup/status, GET /vesper/setup/openrouter-free/models,
POST /vesper/setup/openrouter-free. Upstream HTTP is faked; HERMES_HOME is a temp dir."""

import logging
import os
import stat

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import setup_guide as api_server_setup
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
KEY = "sk-or-v1-" + "a1b2c3d4e5f6a7b8" * 4
OTHER_KEY = "sk-or-v1-" + "9f8e7d6c5b4a3921" * 4
MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # save_env_value publishes into os.environ: register the variable so teardown removes it again.
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    return tmp_path


@pytest.fixture(autouse=True)
def fixture_preferences(monkeypatch):
    """Pin the preference list to the families in UPSTREAM_MODELS, so retuning the real list isn't a test failure."""
    monkeypatch.setattr(api_server_setup, "PREFERRED_FREE_MODELS",
                        ("qwen/qwen3-coder", "deepseek/deepseek-", "nvidia/nemotron-3-ultra"))


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def model(model_id, context=131072, prompt="0", completion="0", tools=True, **extra):
    entry = {"id": model_id, "name": f"Name of {model_id}", "context_length": context,
             "pricing": {"prompt": prompt, "completion": completion},
             "supported_parameters": ["temperature", "tools", "tool_choice"] if tools else ["temperature"]}
    entry.update(extra)
    return entry


UPSTREAM_MODELS = [
    model("acme/small-chat:free", 65536),
    model("meta/big-context:free", 500000),
    model("inclusionai/ling-flash", 262144),                       # free by price, no ":free" suffix
    model("deepseek/deepseek-r1-distill-llama-8b:free", 131072),   # preferred family, but a small variant
    model("deepseek/deepseek-chat-v3:free", 65536),
    model("nvidia/nemotron-3-ultra-550b:free", 1000000),
    model("qwen/qwen3-coder:free", 262144),
    model("openai/gpt-5", 400000, prompt="0.000005", completion="0.00002"),              # paid
    model("openrouter/auto", 2000000, prompt="-1", completion="-1"),                      # dynamic price
    model("acme/no-tools:free", 262144, tools=False),
    model("acme/short-context:free", 8192),
    model("acme/retired:free", 262144, expiration_date="2020-01-01"),
    model("acme/audio-out:free", 262144, architecture={"output_modalities": ["audio"]}),
    model("acme/garbled-price", 262144, prompt="free", completion="free"),
    {"id": 5}, None, "not-a-model",
]
FREE_IDS_IN_ORDER = [
    "qwen/qwen3-coder:free", "deepseek/deepseek-chat-v3:free", "deepseek/deepseek-r1-distill-llama-8b:free",
    "nvidia/nemotron-3-ultra-550b:free", "meta/big-context:free", "inclusionai/ling-flash",
    "acme/small-chat:free"]


class FakeOpenRouter:
    """Stands in for aiohttp.ClientSession: answers the model list and the key check, records the calls."""

    def __init__(self, models=None, key_status=200, models_status=200, unreachable=()):
        self.models = UPSTREAM_MODELS if models is None else models
        self.key_status = key_status
        self.models_status = models_status
        self.unreachable = unreachable  # URLs whose request raises a connection error
        self.calls = []                 # (url, headers)
        self._url = None

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if url in self.unreachable:
            raise aiohttp.ClientConnectionError("connection refused")
        self._url = url
        return self

    @property
    def status(self):
        return self.key_status if self._url == KEY_URL else self.models_status

    async def json(self):
        return {"data": self.models} if self._url == MODELS_URL else {"data": {"label": "sk-or-v1-abc...xyz"}}

    def urls(self):
        return [url for url, _ in self.calls]


def fake_openrouter(monkeypatch, **kwargs):
    fake = FakeOpenRouter(**kwargs)
    monkeypatch.setattr(aiohttp, "ClientSession", fake)
    return fake


def read_config(home):
    import yaml
    return yaml.safe_load((home / "config.yaml").read_text())


def write_config(home, text):
    (home / "config.yaml").write_text(text)


async def post_setup(client, body):
    return await client.post("/vesper/setup/openrouter-free", headers=AUTH, json=body)


@pytest.mark.asyncio
async def test_setup_routes_require_auth(monkeypatch):
    fake = fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        for method, path in (("GET", "/vesper/setup/status"), ("GET", "/vesper/setup/openrouter-free/models"),
                             ("POST", "/vesper/setup/openrouter-free")):
            resp = await client.request(method, path, json={"api_key": KEY})
            assert resp.status == 401, (method, path)
            resp = await client.request(method, path, json={"api_key": KEY},
                                        headers={"Authorization": "Bearer wrong-key"})
            assert resp.status == 401, (method, path)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_status_reports_current_model_and_key_presence(hermes_home):
    async with TestClient(TestServer(app())) as client:
        data = await (await client.get("/vesper/setup/status", headers=AUTH)).json()
        assert data == {"provider": "", "model": "", "openrouter_key_set": False}

        write_config(hermes_home, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
        (hermes_home / ".env").write_text(f"OTHER=1\nOPENROUTER_API_KEY={KEY}\n")
        resp = await client.get("/vesper/setup/status", headers=AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data == {"provider": "openai-codex", "model": "gpt-5.5", "openrouter_key_set": True}
        assert KEY not in await resp.text()


@pytest.mark.asyncio
async def test_free_models_are_filtered_and_ranked(monkeypatch):
    fake = fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)
        assert resp.status == 200
        data = await resp.json()
    assert [m["id"] for m in data["models"]] == FREE_IDS_IN_ORDER
    assert data["recommended"] == "qwen/qwen3-coder:free"
    assert data["models"][0] == {"id": "qwen/qwen3-coder:free", "name": "Name of qwen/qwen3-coder:free",
                                 "context_length": 262144}
    assert fake.urls() == [MODELS_URL]
    assert "Authorization" not in fake.calls[0][1]  # the public list needs no key


@pytest.mark.asyncio
async def test_without_a_preferred_family_the_longest_context_wins(monkeypatch):
    fake_openrouter(monkeypatch, models=[model("acme/a:free", 65536), model("acme/b:free", 262144),
                                         model("acme/c:free", 262144), model("acme/d:free", 32000),
                                         model("acme/e:free", 31999)])
    async with TestClient(TestServer(app())) as client:
        data = await (await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)).json()
    assert [m["id"] for m in data["models"]] == ["acme/b:free", "acme/c:free", "acme/a:free", "acme/d:free"]
    assert data["recommended"] == "acme/b:free"


@pytest.mark.asyncio
async def test_free_models_are_cached_for_ten_minutes(monkeypatch):
    fake = fake_openrouter(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(api_server_setup, "_now", lambda: clock[0])
    async with TestClient(TestServer(app())) as client:
        first = await (await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)).json()
        clock[0] += 599
        again = await (await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)).json()
        assert again == first and fake.urls() == [MODELS_URL]

        clock[0] += 2   # past the 10 minute mark
        fake.models = [model("acme/new:free")]
        refreshed = await (await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)).json()
        assert [m["id"] for m in refreshed["models"]] == ["acme/new:free"]
        assert fake.urls() == [MODELS_URL, MODELS_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("fake_kwargs", [
    {"models_status": 503}, {"unreachable": (MODELS_URL,)}, {"models": []}, {"models": "oops"},
    {"models": [model("acme/paid", prompt="0.1", completion="0.1")]}])
async def test_free_models_upstream_failure_is_a_502_and_not_cached(monkeypatch, fake_kwargs):
    fake = fake_openrouter(monkeypatch, **fake_kwargs)
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)
        assert resp.status == 502
        assert (await resp.json())["error"]["code"] == "upstream_unavailable"

        fake.models, fake.models_status, fake.unreachable = UPSTREAM_MODELS, 200, ()
        resp = await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)
        assert resp.status == 200  # the failure was not remembered


@pytest.mark.asyncio
async def test_setup_saves_key_and_model_without_echoing_the_key(hermes_home, monkeypatch, caplog):
    fake = fake_openrouter(monkeypatch)
    (hermes_home / ".env").write_text("TELEGRAM_BOT_TOKEN=keep-me\n")
    (hermes_home / ".env").chmod(0o600)
    write_config(hermes_home, "terminal:\n  backend: local\n")
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": f"  {KEY} ", "model": "nvidia/nemotron-3-ultra-550b:free"})
        text = await resp.text()
        assert resp.status == 200, text
        data = await resp.json()
        assert data["ok"] is True and data["provider"] == "openrouter"
        assert data["model"] == "nvidia/nemotron-3-ultra-550b:free"
        assert "20 requests per minute" in data["limits"] and "50 requests per day" in data["limits"]
        assert set(data) == {"ok", "provider", "model", "limits"}

        status = await (await client.get("/vesper/setup/status", headers=AUTH)).json()
        assert status == {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b:free",
                          "openrouter_key_set": True}
        assert "keep-me" not in await (await client.get("/vesper/setup/status", headers=AUTH)).text()

    # The key was verified upstream with a Bearer header, once.
    assert fake.urls().count(KEY_URL) == 1
    assert dict(fake.calls)[KEY_URL] == {"Authorization": f"Bearer {KEY}"}

    # .env: Hermes's own writer -- key stored, other lines kept, owner-only mode.
    env = hermes_home / ".env"
    lines = env.read_text().splitlines()
    assert "TELEGRAM_BOT_TOKEN=keep-me" in lines
    assert [line for line in lines if line.startswith("OPENROUTER_API_KEY=")] == [f"OPENROUTER_API_KEY={KEY}"]
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600

    # config.yaml: the same shape `hermes model` writes for OpenRouter; unrelated settings survive.
    section = read_config(hermes_home)["model"]
    assert section["provider"] == "openrouter" and section["default"] == "nvidia/nemotron-3-ultra-550b:free"
    assert section["base_url"] == "https://openrouter.ai/api/v1" and section["api_mode"] == "chat_completions"
    assert read_config(hermes_home)["terminal"] == {"backend": "local"}

    # The key is in the response nowhere, nor in any log record.
    assert KEY not in text
    assert KEY not in caplog.text


@pytest.mark.asyncio
async def test_new_env_file_is_owner_only(hermes_home, monkeypatch):
    fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        assert (await post_setup(client, {"api_key": KEY})).status == 200
    env = hermes_home / ".env"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert env.read_text().strip() == f"OPENROUTER_API_KEY={KEY}"


@pytest.mark.asyncio
async def test_model_defaults_to_the_recommended_one(hermes_home, monkeypatch):
    fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        data = await (await post_setup(client, {"api_key": KEY})).json()
        recommended = (await (await client.get("/vesper/setup/openrouter-free/models", headers=AUTH)).json())["recommended"]
    assert data["model"] == recommended == "qwen/qwen3-coder:free"
    assert read_config(hermes_home)["model"]["default"] == "qwen/qwen3-coder:free"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", [
    "not-a-key", "sk-ant-api03-" + "x" * 40, "sk-or-", "sk-or-short", "sk-or-v1-" + "a" * 20 + " " + "b" * 20,
    "sk-or-v1-" + "a" * 20 + "\n" + "b" * 20, "sk-or-v1-" + "é" * 30, "sk-or-v1-" + "a" * 300, 12345, ["sk-or-v1"]])
async def test_malformed_keys_are_rejected_before_any_upstream_call(hermes_home, monkeypatch, bad_key):
    fake = fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": bad_key})
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_key"
    assert fake.calls == []
    assert not (hermes_home / ".env").exists() and not (hermes_home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_a_key_openrouter_rejects_is_not_saved(hermes_home, monkeypatch, caplog):
    fake = fake_openrouter(monkeypatch, key_status=401)
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": KEY})
        text = await resp.text()
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_key"
        assert KEY not in text and KEY not in caplog.text
        fake.key_status = 403
        assert (await post_setup(client, {"api_key": KEY})).status == 400
        assert (await client.get("/vesper/setup/status", headers=AUTH)).status == 200
    assert not (hermes_home / ".env").exists() and not (hermes_home / "config.yaml").exists()
    assert "OPENROUTER_API_KEY" not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize("fake_kwargs", [{"key_status": 500}, {"unreachable": (KEY_URL,)}])
async def test_key_check_outage_is_a_502_and_saves_nothing(hermes_home, monkeypatch, fake_kwargs, caplog):
    fake_openrouter(monkeypatch, **fake_kwargs)
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": KEY})
        assert resp.status == 502
        assert (await resp.json())["error"]["code"] == "upstream_unavailable"
        assert KEY not in await resp.text()
    assert KEY not in caplog.text
    assert not (hermes_home / ".env").exists() and not (hermes_home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_key_is_required_when_none_is_configured(hermes_home, monkeypatch):
    fake = fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        for body in ({}, {"api_key": ""}, {"api_key": "   "}, {"model": "qwen/qwen3-coder:free"}):
            resp = await post_setup(client, body)
            assert resp.status == 400, body
            assert (await resp.json())["error"]["code"] == "missing_key"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_configured_key_is_reused_and_checked(hermes_home, monkeypatch):
    fake = fake_openrouter(monkeypatch)
    (hermes_home / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\nOTHER=1\n")
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"model": "meta/big-context:free"})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["model"] == "meta/big-context:free"
    assert dict(fake.calls)[KEY_URL] == {"Authorization": f"Bearer {KEY}"}
    assert (hermes_home / ".env").read_text() == f"OPENROUTER_API_KEY={KEY}\nOTHER=1\n"
    assert read_config(hermes_home)["model"]["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_a_rejected_saved_key_asks_for_a_new_one(hermes_home, monkeypatch):
    fake_openrouter(monkeypatch, key_status=401)
    (hermes_home / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {})
        assert resp.status == 400
        body = await resp.json()
        assert body["error"]["code"] == "invalid_key" and KEY not in await resp.text()
    assert not (hermes_home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_saved_key_that_cannot_be_rechecked_is_still_used(hermes_home, monkeypatch):
    fake_openrouter(monkeypatch, unreachable=(KEY_URL,))
    (hermes_home / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    async with TestClient(TestServer(app())) as client:
        assert (await post_setup(client, {})).status == 200


@pytest.mark.asyncio
async def test_a_new_key_replaces_the_saved_one(hermes_home, monkeypatch):
    fake = fake_openrouter(monkeypatch)
    (hermes_home / ".env").write_text(f"A=1\nOPENROUTER_API_KEY={KEY}\nB=2\n")
    async with TestClient(TestServer(app())) as client:
        assert (await post_setup(client, {"api_key": OTHER_KEY})).status == 200
    assert (hermes_home / ".env").read_text() == f"A=1\nOPENROUTER_API_KEY={OTHER_KEY}\nB=2\n"
    assert dict(fake.calls)[KEY_URL] == {"Authorization": f"Bearer {OTHER_KEY}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_model", [
    "openai/gpt-5", "acme/no-tools:free", "acme/short-context:free", "openrouter/auto", "qwen/qwen3-coder",
    "does/not-exist:free", "../etc/passwd", 7, ["qwen/qwen3-coder:free"]])
async def test_invalid_model_is_rejected_and_nothing_is_saved(hermes_home, monkeypatch, bad_model):
    fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": KEY, "model": bad_model})
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_model"
    assert not (hermes_home / ".env").exists() and not (hermes_home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_setup_needs_the_model_list_and_reports_an_outage(hermes_home, monkeypatch):
    fake_openrouter(monkeypatch, models_status=503)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": KEY})
        assert resp.status == 502 and (await resp.json())["error"]["code"] == "upstream_unavailable"
    assert not (hermes_home / ".env").exists()


@pytest.mark.asyncio
async def test_bad_bodies_are_400s(monkeypatch):
    fake_openrouter(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/openrouter-free", headers=AUTH, data="{not json")
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_json"
        resp = await post_setup(client, ["not", "an", "object"])
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_a_locked_config_is_reported_instead_of_claiming_success(hermes_home, monkeypatch):
    """Managed installs refuse config writes silently; the route must notice rather than say ok."""
    fake_openrouter(monkeypatch)
    from hermes_cli import config as hermes_config
    monkeypatch.setattr(hermes_config, "save_config", lambda *a, **k: None)
    async with TestClient(TestServer(app())) as client:
        resp = await post_setup(client, {"api_key": KEY})
        assert resp.status == 500
        body = await resp.json()
        assert body["error"]["code"] == "save_failed" and KEY not in await resp.text()


@pytest.mark.asyncio
async def test_next_turn_resolves_to_the_new_provider_without_a_restart(hermes_home, monkeypatch):
    """The gateway builds each turn's agent from config.yaml + .env on demand (api_server._create_agent calls
    these two helpers), so a running process sees the new setup on the very next turn."""
    import gateway.run
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
    # gateway.run captures HERMES_HOME when first imported (by whichever test got there first).
    monkeypatch.setattr(gateway.run, "_hermes_home", hermes_home)
    fake_openrouter(monkeypatch)
    write_config(hermes_home, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    assert _resolve_gateway_model() == "gpt-5.5"
    async with TestClient(TestServer(app())) as client:
        assert (await post_setup(client, {"api_key": KEY, "model": "qwen/qwen3-coder:free"})).status == 200
    runtime = _resolve_runtime_agent_kwargs()
    assert runtime["provider"] == "openrouter"
    assert runtime["base_url"] == "https://openrouter.ai/api/v1"
    assert runtime["api_key"] == KEY
    assert _resolve_gateway_model() == "qwen/qwen3-coder:free"
