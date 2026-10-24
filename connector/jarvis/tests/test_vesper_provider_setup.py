"""Terminal-free provider setup for the dashboard guide: API keys, device-code sign-ins and local model servers.
Upstream HTTP is faked; HERMES_HOME is a temp dir; config and runtime resolution are Hermes' real code."""

import aiohttp
import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import setup_providers as setup
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
ANTHROPIC_KEY = "sk-ant-api03-" + "a1b2c3d4" * 8
OPENAI_KEY = "sk-proj-" + "f9e8d7c6" * 8
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY")


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in KEY_VARS:   # save_env_value publishes into os.environ: register each so teardown removes it
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)
    return tmp_path


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


class FakeProvider:
    """Stands in for aiohttp.ClientSession during the key check; records (url, headers)."""

    def __init__(self, status=200, unreachable=False):
        self._status, self.unreachable, self.calls = status, unreachable, []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if self.unreachable:
            raise aiohttp.ClientConnectionError("connection refused")
        return self

    @property
    def status(self):
        return self._status


def fake_provider(monkeypatch, **kwargs):
    fake = FakeProvider(**kwargs)
    monkeypatch.setattr(aiohttp, "ClientSession", fake)
    return fake


def read_config(home):
    path = home / "config.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def runtime(home, monkeypatch):
    """What the next chat turn resolves to (the same helpers api_server._create_agent calls)."""
    import gateway.run
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
    monkeypatch.setattr(gateway.run, "_hermes_home", home)
    return _resolve_runtime_agent_kwargs(), _resolve_gateway_model()


@pytest.mark.asyncio
async def test_every_route_requires_the_dashboard_key(monkeypatch):
    fake = fake_provider(monkeypatch)
    routes = (("GET", "/vesper/setup/providers"), ("POST", "/vesper/setup/key"),
              ("POST", "/vesper/setup/login/nous/start"), ("GET", "/vesper/setup/login/nous/poll/x"),
              ("DELETE", "/vesper/setup/login/nous/x"), ("GET", "/vesper/setup/local/scan"),
              ("POST", "/vesper/setup/local/probe"), ("POST", "/vesper/setup/local"))
    async with TestClient(TestServer(app())) as client:
        for method, path in routes:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                resp = await client.request(method, path, json={"provider": "anthropic", "api_key": ANTHROPIC_KEY},
                                            headers=headers)
                assert resp.status == 401, (method, path)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_provider_list_puts_featured_first_and_reflects_saved_keys(hermes_home, monkeypatch):
    fake_provider(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        before = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
        ids = [p["id"] for p in before["keys"]]
        featured = [p["id"] for p in before["keys"] if p["featured"]]
        assert ids[:len(featured)] == featured and "anthropic" in featured
        assert not set(ids) & setup.EXCLUDED_KEY_PROVIDERS
        assert {p["id"] for p in before["logins"]} == set(setup.LOGIN_PROVIDERS)
        assert all(p["signup_url"].startswith("https://") for p in before["keys"] if p["featured"])
        assert not next(p for p in before["keys"] if p["id"] == "anthropic")["connected"]
        monkeypatch.setattr(setup, "_default_model", lambda pid: "claude-test-1")
        assert (await client.post("/vesper/setup/key", headers=AUTH,
                                  json={"provider": "anthropic", "api_key": ANTHROPIC_KEY})).status == 200
        after = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
    assert next(p for p in after["keys"] if p["id"] == "anthropic")["connected"]
    assert after["current"] == {"provider": "anthropic", "model": "claude-test-1"}


@pytest.mark.asyncio
async def test_an_accepted_key_is_saved_and_the_next_turn_uses_it(hermes_home, monkeypatch):
    fake = fake_provider(monkeypatch)
    monkeypatch.setattr(setup, "_default_model", lambda pid: "gpt-test-1")
    (hermes_home / "config.yaml").write_text("model:\n  default: old-model\n  provider: openrouter\n")
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH,
                                 json={"provider": "openai-api", "api_key": OPENAI_KEY})
        text = await resp.text()
    assert resp.status == 200 and OPENAI_KEY not in text
    assert (await resp.json())["model"] == "gpt-test-1"
    assert fake.calls[0][0] == "https://api.openai.com/v1/models"
    assert OPENAI_KEY not in (hermes_home / "config.yaml").read_text()
    kwargs, model = runtime(hermes_home, monkeypatch)
    assert (kwargs["provider"], kwargs["api_key"], model) == ("openai-api", OPENAI_KEY, "gpt-test-1")


@pytest.mark.asyncio
async def test_anthropic_keys_are_checked_with_anthropics_header_and_drop_a_stale_base_url(hermes_home, monkeypatch):
    fake = fake_provider(monkeypatch)
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: x\n  provider: custom\n  base_url: http://old-host:1234/v1\n")
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH,
                                 json={"provider": "anthropic", "api_key": ANTHROPIC_KEY, "model": "claude-chosen"})
    assert resp.status == 200
    url, headers = fake.calls[0]
    assert url == "https://api.anthropic.com/v1/models" and headers["x-api-key"] == ANTHROPIC_KEY
    model_cfg = read_config(hermes_home)["model"]
    assert model_cfg["provider"] == "anthropic" and model_cfg["default"] == "claude-chosen"
    assert "base_url" not in model_cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_saves_nothing(hermes_home, monkeypatch, status):
    fake_provider(monkeypatch, status=status)
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH,
                                 json={"provider": "openai-api", "api_key": OPENAI_KEY})
        body = await resp.json()
    assert resp.status == 400 and body["error"]["code"] == "invalid_key"
    assert not (hermes_home / ".env").exists() or OPENAI_KEY not in (hermes_home / ".env").read_text()
    assert read_config(hermes_home).get("model", {}).get("provider") != "openai-api"


@pytest.mark.asyncio
async def test_a_key_that_cant_be_checked_is_still_saved(hermes_home, monkeypatch):
    """Many providers have no /models route, and an outage shouldn't block setup: only a refusal does."""
    fake_provider(monkeypatch, unreachable=True)
    monkeypatch.setattr(setup, "_default_model", lambda pid: "gpt-test-1")
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH,
                                 json={"provider": "openai-api", "api_key": OPENAI_KEY})
    assert resp.status == 200
    assert read_config(hermes_home)["model"]["provider"] == "openai-api"


@pytest.mark.asyncio
async def test_a_free_tier_gemini_key_is_refused(hermes_home, monkeypatch):
    fake_provider(monkeypatch)
    import agent.gemini_native_adapter as gemini
    monkeypatch.setattr(gemini, "probe_gemini_tier", lambda key, base: "free")
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH,
                                 json={"provider": "gemini", "api_key": "AIza" + "x1" * 18})
        body = await resp.json()
    assert resp.status == 400 and body["error"]["code"] == "free_tier_key"
    assert read_config(hermes_home).get("model", {}).get("provider") != "gemini"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    ({"provider": "openrouter", "api_key": OPENAI_KEY}, "invalid_provider"),   # the Free tab's job
    ({"provider": "no-such", "api_key": OPENAI_KEY}, "invalid_provider"),
    ({"provider": "openai-api"}, "missing_key"),
    ({"provider": "openai-api", "api_key": "two words here"}, "invalid_key"),
])
async def test_bad_key_requests_are_refused_before_any_upstream_call(monkeypatch, body, code):
    fake = fake_provider(monkeypatch)
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/key", headers=AUTH, json=body)
        assert resp.status == 400 and (await resp.json())["error"]["code"] == code
    assert fake.calls == []


@pytest.fixture
def fake_device_flow(monkeypatch):
    """The Hermes dashboard's device-code start, minus the network: registers a pending session."""
    import hermes_cli.web_routers.oauth as oauth

    async def start(provider_id, profile=None):
        sid, _sess = oauth._new_oauth_session(provider_id, "device_code", profile=profile)
        return {"session_id": sid, "flow": "device_code", "user_code": "ABCD-1234",
                "verification_url": "https://auth.example/device", "expires_in": 900, "poll_interval": 5}
    monkeypatch.setattr(oauth, "_start_device_code_flow", start)


def set_status(session_id, status):
    from hermes_cli.web_server_oauth import _oauth_sessions, _oauth_sessions_lock
    with _oauth_sessions_lock:
        _oauth_sessions[session_id]["status"] = status


@pytest.mark.asyncio
async def test_a_sign_in_shows_the_code_then_activates_the_providers_model_once(hermes_home, monkeypatch,
                                                                              fake_device_flow):
    picks = []
    monkeypatch.setattr(setup, "_default_model", lambda pid: picks.append(pid) or "gpt-codex-test")
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/login/openai-codex/start", headers=AUTH)).json()
        assert started["user_code"] == "ABCD-1234" and started["verification_url"].startswith("https://")
        poll = f"/vesper/setup/login/openai-codex/poll/{started['session_id']}"
        assert (await (await client.get(poll, headers=AUTH)).json())["status"] == "pending"
        set_status(started["session_id"], "approved")
        first = await (await client.get(poll, headers=AUTH)).json()
        second = await (await client.get(poll, headers=AUTH)).json()
        # a session belongs to its provider
        assert (await client.get(f"/vesper/setup/login/nous/poll/{started['session_id']}", headers=AUTH)).status == 404
    assert first["status"] == "approved" and first["model"] == "gpt-codex-test" == second["model"]
    assert picks == ["openai-codex"]
    model_cfg = read_config(hermes_home)["model"]
    assert (model_cfg["provider"], model_cfg["default"]) == ("openai-codex", "gpt-codex-test")


@pytest.mark.asyncio
async def test_a_denied_sign_in_changes_nothing_and_a_cancelled_one_is_gone(hermes_home, monkeypatch,
                                                                          fake_device_flow):
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/login/xai-oauth/start", headers=AUTH)).json()
        set_status(started["session_id"], "denied")
        poll = f"/vesper/setup/login/xai-oauth/poll/{started['session_id']}"
        assert (await (await client.get(poll, headers=AUTH)).json())["status"] == "denied"
        other = await (await client.post("/vesper/setup/login/xai-oauth/start", headers=AUTH)).json()
        cancel = await client.delete(f"/vesper/setup/login/xai-oauth/{other['session_id']}", headers=AUTH)
        assert (await cancel.json())["ok"] is True
        assert (await client.get(f"/vesper/setup/login/xai-oauth/poll/{other['session_id']}", headers=AUTH)).status == 404
        assert (await client.post("/vesper/setup/login/anthropic/start", headers=AUTH)).status == 400
    assert read_config(hermes_home).get("model", {}).get("provider") != "xai-oauth"


@pytest.fixture
def fake_local_server(monkeypatch):
    """probe_api_models answering only for the given base URLs; records what was probed."""
    import hermes_cli.models as models
    servers, probed = {}, []

    def probe(api_key, base_url, timeout=5.0, **kwargs):
        probed.append(base_url)
        found = servers.get(base_url.rstrip("/"))
        return {"models": found, "probed_url": f"{base_url}/models", "resolved_base_url": base_url,
                "used_fallback": False, "suggested_base_url": None}
    monkeypatch.setattr(models, "probe_api_models", probe)
    return servers, probed


@pytest.mark.asyncio
async def test_scan_finds_a_local_server_and_connecting_makes_it_main(hermes_home, monkeypatch, fake_local_server):
    servers, _probed = fake_local_server
    servers["http://host.docker.internal:11434/v1"] = ["qwen3:8b", "llama3.3:70b"]
    async with TestClient(TestServer(app())) as client:
        found = (await (await client.get("/vesper/setup/local/scan", headers=AUTH)).json())["found"]
        assert found == [{"name": "Ollama", "base_url": "http://host.docker.internal:11434/v1",
                          "models": ["llama3.3:70b", "qwen3:8b"]}]
        resp = await client.post("/vesper/setup/local", headers=AUTH,
                                 json={"base_url": found[0]["base_url"], "model": "qwen3:8b"})
        body = await resp.json()
    assert resp.status == 200 and body["model"] == "qwen3:8b"
    kwargs, model = runtime(hermes_home, monkeypatch)
    assert kwargs["base_url"].rstrip("/") == "http://host.docker.internal:11434/v1" and model == "qwen3:8b"


@pytest.mark.asyncio
async def test_a_local_servers_key_goes_to_env_not_config(hermes_home, monkeypatch, fake_local_server):
    servers, _probed = fake_local_server
    servers["http://10.0.0.50:1234/v1"] = ["local-model"]
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/local", headers=AUTH,
                                 json={"base_url": "http://10.0.0.50:1234/v1", "api_key": "lm-secret-key-123"})
        body = await resp.json()
    assert resp.status == 200 and body["model"] == "local-model"
    assert "lm-secret-key-123" not in (hermes_home / "config.yaml").read_text()
    assert "lm-secret-key-123" in (hermes_home / ".env").read_text()


@pytest.mark.asyncio
async def test_a_bare_address_means_ollamas_default_port(monkeypatch, fake_local_server):
    servers, probed = fake_local_server
    servers["http://192.168.1.20:11434"] = ["m"]
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/local/probe", headers=AUTH, json={"base_url": "192.168.1.20"})
        assert resp.status == 200 and (await resp.json())["models"] == ["m"]
        missing = await client.post("/vesper/setup/local/probe", headers=AUTH, json={"base_url": "10.9.9.9:8080"})
        assert missing.status == 502 and (await missing.json())["error"]["code"] == "not_reachable"
        bad = await client.post("/vesper/setup/local/probe", headers=AUTH, json={"base_url": "ftp://x"})
        assert bad.status == 400
    assert probed[:2] == ["http://192.168.1.20:11434", "http://10.9.9.9:8080"]


@pytest.mark.asyncio
async def test_a_server_without_models_is_explained(hermes_home, monkeypatch, fake_local_server):
    servers, _probed = fake_local_server
    servers["http://10.0.0.50:11434"] = []
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/local", headers=AUTH, json={"base_url": "10.0.0.50"})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "no_models"
    assert read_config(hermes_home).get("model", {}).get("default") is None


def test_a_nous_sign_in_picks_a_model_the_account_can_use(hermes_home, monkeypatch):
    """Nous filters its catalog by the account's tier; the guide must not land on a model the tier can't use."""
    import hermes_cli.auth as auth
    import hermes_cli.model_setup_flows as flows
    import hermes_cli.models as models
    import hermes_cli.models_pricing as pricing
    monkeypatch.setattr(models, "get_curated_nous_model_ids", lambda: ["paid/model", "free/model"])
    monkeypatch.setattr(models, "check_nous_free_tier", lambda **kw: True)
    monkeypatch.setattr(models, "get_preferred_silent_default_model", lambda provider="openrouter": "paid/model")
    monkeypatch.setattr(pricing, "get_pricing_for_provider", lambda pid: {})
    monkeypatch.setattr(auth, "get_provider_auth_state", lambda pid: {"portal_base_url": "https://portal.example"})
    monkeypatch.setattr(flows, "_nous_model_catalog",
                        lambda free, portal, ids, prices: (ids, prices, ["paid/model"], "Upgrade to use it", False))
    monkeypatch.setattr(auth, "resolve_nous_runtime_credentials",
                        lambda **kw: {"base_url": "https://inference.example/v1", "api_key": "nous-key"})
    model, problem = setup._activate_login("nous")
    assert (model, problem) == ("free/model", None)
    model_cfg = read_config(hermes_home)["model"]
    assert (model_cfg["provider"], model_cfg["default"], model_cfg["base_url"]) == (
        "nous", "free/model", "https://inference.example/v1")
