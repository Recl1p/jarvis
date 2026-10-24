"""Claude Pro/Max sign-in for the setup guide: the dashboard downloads Claude Code on first use, then drives its own
`claude setup-token` on a pty. A stand-in `claude` script plays setup-token (prints the sign-in link, reads the pasted
code, prints a token or an error) and is "downloaded" from a local file; everything else is real: the download
check, the pty driver, the .env/config writes and the next turn's runtime resolution."""

import base64
import hashlib
import io
import os
import platform
import sys
import tarfile
import time

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import setup_claude as claude
from jarvis import setup_providers as providers
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
TOKEN = "sk-ant-oat01-" + "Q7w9E2r4T6y8" * 4
GOOD_CODE = "good-code-123#state456"
LONG_GOOD_CODE = "Lc0de" * 24 + "#" + "st4te" * 9      # a real code's length (165 chars)
# Like Claude Code, the stand-in reads raw keystrokes and treats a long burst as a paste, in which an Enter is text.
FAKE_SETUP_TOKEN = f"""#!{sys.executable}
import os, sys, time, tty
open(os.path.join(os.environ["HOME"], "started"), "w").write(str(os.getpid()))
print("Welcome to Claude Code")
print("Browser didn't open? Use the url below to sign in")
print("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&response_type=code&state=state456")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
tty.setraw(0)
typed = ""
while True:
    chunk = os.read(0, 4096).decode()
    if chunk == "\\r" or (chunk.endswith("\\r") and len(chunk) <= 64):
        typed += chunk[:-1]
        break
    typed += chunk
code = typed.strip()
sys.stdout.write("\\r\\n" + "*" * len(code) + "\\r\\n")
if code in ({GOOD_CODE!r}, {LONG_GOOD_CODE!r}):
    sys.stdout.write("Your OAuth token (valid for 1 year): {TOKEN}\\r\\n")
else:
    sys.stdout.write("OAuth error: Invalid code\\r\\n")
sys.stdout.flush()
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)
    return home


@pytest.fixture(autouse=True)
def no_claude_on_path(monkeypatch):
    monkeypatch.setattr(claude.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude, "_install", {"state": "idle", "done": 0, "total": 0, "error": ""})


def installed_path(home):
    return home / "claude-code" / claude.CLAUDE_CODE_VERSION / "claude"


@pytest.fixture
def fake_claude(hermes_home, monkeypatch):
    """Claude Code already downloaded (the stand-in, where the download puts it)."""
    path = installed_path(hermes_home)
    path.parent.mkdir(parents=True)
    path.write_text(FAKE_SETUP_TOKEN)
    path.chmod(0o755)
    monkeypatch.setattr(providers, "_default_model", lambda pid: "claude-test-model")
    monkeypatch.setattr(claude, "START_TIMEOUT", 20)
    monkeypatch.setattr(claude, "SUBMIT_TIMEOUT", 10)
    return path


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def login_homes():
    with claude._lock:
        return [s.home for s in claude._sessions.values()]


@pytest.mark.asyncio
async def test_routes_require_the_dashboard_key(fake_claude):
    async with TestClient(TestServer(app())) as client:
        for method, path in (("POST", "/vesper/setup/claude/start"), ("POST", "/vesper/setup/claude/submit"),
                             ("DELETE", "/vesper/setup/claude/x")):
            assert (await client.request(method, path, json={})).status == 401
    assert login_homes() == []


def package(tmp_path, content=FAKE_SETUP_TOKEN):
    """An npm-style tarball holding package/claude, and its SRI."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = content.encode()
        info = tarfile.TarInfo("package/claude")
        info.size, info.mode = len(data), 0o644
        tar.addfile(info, io.BytesIO(data))
    path = tmp_path / f"claude-code-linux-test-{claude.CLAUDE_CODE_VERSION}.tgz"
    path.write_bytes(buf.getvalue())
    return path, "sha512-" + base64.b64encode(hashlib.sha512(buf.getvalue()).digest()).decode()


def serve_package(monkeypatch, path, sri):
    monkeypatch.setattr(claude, "CLAUDE_CODE_URL", path.parent.as_uri() + "/claude-code-{suffix}-{version}.tgz")
    monkeypatch.setattr(claude, "CLAUDE_CODE_BUILDS", {platform.machine(): ("linux-test", sri, 0)})


async def wait_installed(client, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = await (await client.get("/vesper/setup/claude/status", headers=AUTH)).json()
        if status["installed"] or status["state"] == "error":
            return status
        time.sleep(0.1)
    pytest.fail("the download never finished")


@pytest.mark.asyncio
async def test_before_the_download_sign_in_asks_for_it(hermes_home):
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/claude/start", headers=AUTH)
        assert resp.status == 409 and (await resp.json())["error"]["code"] == "not_installed"
        listing = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
    assert listing["claude"]["installed"] is False


@pytest.mark.asyncio
async def test_the_download_is_verified_installed_once_and_replaces_old_versions(hermes_home, tmp_path, monkeypatch):
    path, sri = package(tmp_path)
    serve_package(monkeypatch, path, sri)
    old = hermes_home / "claude-code" / "0.0.1"
    old.mkdir(parents=True)
    (old / "claude").write_text("old")
    monkeypatch.setattr(providers, "_default_model", lambda pid: "claude-test-model")
    async with TestClient(TestServer(app())) as client:
        first = await (await client.post("/vesper/setup/claude/install", headers=AUTH)).json()
        assert first["state"] == "downloading" and not first["installed"]
        status = await wait_installed(client)
        assert status["installed"] and status["done"] == path.stat().st_size
        again = await (await client.post("/vesper/setup/claude/install", headers=AUTH)).json()
        assert again["installed"] and again["state"] == "idle"          # no second download
        started = await client.post("/vesper/setup/claude/start", headers=AUTH)   # and the sign-in carries on
        assert started.status == 200
        await client.delete(f"/vesper/setup/claude/{(await started.json())['session_id']}", headers=AUTH)
    assert os.access(installed_path(hermes_home), os.X_OK)
    assert not old.exists()
    assert sorted(p.name for p in installed_path(hermes_home).parent.iterdir()) == ["claude"]


@pytest.mark.asyncio
async def test_a_download_that_fails_its_checksum_installs_nothing(hermes_home, tmp_path, monkeypatch):
    path, _sri = package(tmp_path)
    serve_package(monkeypatch, path, "sha512-" + base64.b64encode(b"x" * 64).decode())
    async with TestClient(TestServer(app())) as client:
        await client.post("/vesper/setup/claude/install", headers=AUTH)
        status = await wait_installed(client)
    assert status["state"] == "error" and "checksum" in status["error"]
    assert not installed_path(hermes_home).exists()
    assert list(installed_path(hermes_home).parent.iterdir()) == []      # no half-downloaded leftovers


@pytest.mark.asyncio
async def test_sign_in_saves_the_plan_token_and_the_next_turn_uses_it(hermes_home, fake_claude, monkeypatch):
    async with TestClient(TestServer(app())) as client:
        listing = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
        assert listing["claude"]["installed"] is True
        started = await (await client.post("/vesper/setup/claude/start", headers=AUTH)).json()
        assert started["url"].startswith("https://claude.com/cai/oauth/authorize?")
        [home] = login_homes()
        resp = await client.post("/vesper/setup/claude/submit", headers=AUTH,
                                 json={"session_id": started["session_id"], "code": GOOD_CODE})
        text = await resp.text()
    assert resp.status == 200 and TOKEN not in text
    assert (await resp.json())["model"] == "claude-test-model"
    assert not os.path.exists(home) and login_homes() == []        # the throwaway HOME is gone
    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert config["model"]["provider"] == "anthropic" and TOKEN not in (hermes_home / "config.yaml").read_text()
    import gateway.run
    from gateway.run import _resolve_runtime_agent_kwargs
    monkeypatch.setattr(gateway.run, "_hermes_home", hermes_home)
    runtime = _resolve_runtime_agent_kwargs()
    assert (runtime["provider"], runtime["api_key"]) == ("anthropic", TOKEN)


@pytest.mark.asyncio
async def test_a_real_length_code_is_submitted_not_pasted_into_the_prompt(hermes_home, fake_claude):
    """A 100+ character code sent together with its Enter reads as a paste in Claude Code and is never submitted."""
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/claude/start", headers=AUTH)).json()
        resp = await client.post("/vesper/setup/claude/submit", headers=AUTH,
                                 json={"session_id": started["session_id"], "code": LONG_GOOD_CODE})
        body = await resp.json()
    assert resp.status == 200, body
    assert yaml.safe_load((hermes_home / "config.yaml").read_text())["model"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_a_wrong_code_saves_nothing_and_ends_the_sign_in(hermes_home, fake_claude):
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/claude/start", headers=AUTH)).json()
        resp = await client.post("/vesper/setup/claude/submit", headers=AUTH,
                                 json={"session_id": started["session_id"], "code": "wrong-code-999"})
        body = await resp.json()
        again = await client.post("/vesper/setup/claude/submit", headers=AUTH,
                                  json={"session_id": started["session_id"], "code": GOOD_CODE})
    assert resp.status == 400 and body["error"]["code"] == "login_failed"
    assert again.status == 404
    assert not (hermes_home / ".env").exists() or "ANTHROPIC_TOKEN=sk" not in (hermes_home / ".env").read_text()
    assert login_homes() == []


@pytest.mark.asyncio
async def test_cancel_stops_claude_code(fake_claude):
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/claude/start", headers=AUTH)).json()
        [home] = login_homes()
        pid = int(open(os.path.join(home, "started")).read())
        resp = await client.delete(f"/vesper/setup/claude/{started['session_id']}", headers=AUTH)
        assert (await resp.json())["ok"] is True
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("claude setup-token was left running")
    assert not os.path.exists(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", "has spaces in it", "x" * 700])
async def test_a_malformed_code_is_refused_without_ending_the_sign_in(fake_claude, code):
    async with TestClient(TestServer(app())) as client:
        started = await (await client.post("/vesper/setup/claude/start", headers=AUTH)).json()
        resp = await client.post("/vesper/setup/claude/submit", headers=AUTH,
                                 json={"session_id": started["session_id"], "code": code})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_code"
        assert len(login_homes()) == 1
        await client.delete(f"/vesper/setup/claude/{started['session_id']}", headers=AUTH)


@pytest.mark.asyncio
async def test_a_signed_in_plan_shows_connected_and_can_be_used_again_without_signing_in(hermes_home, monkeypatch):
    """An API key isn't a plan sign-in; a saved setup-token is, and 'use' switches back to Claude without Claude Code."""
    from hermes_cli.config import save_anthropic_api_key, save_anthropic_oauth_token, save_env_value
    monkeypatch.setattr(providers, "_default_model", lambda pid: f"{pid}-default")
    async with TestClient(TestServer(app())) as client:
        save_anthropic_api_key("sk-ant-api03-" + "k" * 40, save_fn=save_env_value)
        listing = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
        assert listing["claude"]["connected"] is False
        refused = await client.post("/vesper/setup/claude/use", headers=AUTH)
        assert refused.status == 409 and (await refused.json())["error"]["code"] == "not_connected"
        save_anthropic_oauth_token(TOKEN, save_fn=save_env_value)
        (hermes_home / "config.yaml").write_text("model:\n  default: other\n  provider: openrouter\n")
        listing = await (await client.get("/vesper/setup/providers", headers=AUTH)).json()
        assert listing["claude"]["connected"] is True
        used = await client.post("/vesper/setup/claude/use", headers=AUTH)
        body = await used.json()
    assert used.status == 200 and body["model"] == "anthropic-default" and TOKEN not in str(body)
    model_cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())["model"]
    assert (model_cfg["provider"], model_cfg["default"]) == ("anthropic", "anthropic-default")
    assert login_homes() == []                                   # no Claude Code run was needed


@pytest.mark.asyncio
async def test_a_signed_in_provider_can_be_used_again_without_signing_in(hermes_home, monkeypatch):
    monkeypatch.setattr(providers, "_default_model", lambda pid: "gpt-again")
    monkeypatch.setattr(providers, "_connected", lambda pid: pid == "openai-codex")
    async with TestClient(TestServer(app())) as client:
        refused = await client.post("/vesper/setup/login/nous/use", headers=AUTH)
        used = await client.post("/vesper/setup/login/openai-codex/use", headers=AUTH)
        body = await used.json()
        bad = await client.post("/vesper/setup/login/anthropic/use", headers=AUTH)
    assert refused.status == 409 and bad.status == 400
    assert used.status == 200 and body["model"] == "gpt-again"
    model_cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())["model"]
    assert (model_cfg["provider"], model_cfg["default"]) == ("openai-codex", "gpt-again")
