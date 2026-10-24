"""POST /vesper/doctor: runs `hermes doctor` (never --fix) as a subprocess and returns a cleaned,
redacted report. The subprocess is faked; nothing here runs the real doctor."""

import asyncio
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import setup_guide as api_server_setup
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
SK_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
HEX_RUN = "d41d8cd98f00b204e9800998ecf8427e5a8b3c4d"
B64_RUN = "dGhpcyBpcyBhIHNlY3JldCBwYXlsb2FkIQ0123456789"


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


class FakeProc:
    """An asyncio subprocess stand-in. `hold` makes communicate() wait; `hang` makes it never finish."""

    def __init__(self, output=b"", returncode=0, hold=None, hang=False):
        self.output, self.returncode = output, returncode
        self.hold, self.hang = hold, hang
        self.started = asyncio.Event()
        self.killed = False

    async def communicate(self):
        self.started.set()
        if self.hang:
            await asyncio.sleep(30)
        if self.hold is not None:
            await self.hold.wait()
        return self.output, None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def fake_subprocess(monkeypatch, *procs, error=None):
    """Make asyncio.create_subprocess_exec hand out `procs` in turn; returns the list of recorded launches."""
    launches, queue = [], list(procs)

    async def create_subprocess_exec(*argv, **kwargs):
        launches.append((list(argv), kwargs))
        if error is not None:
            raise error
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    return launches


DOCTOR_OUTPUT = (
    "\x1b[36m┌───────────────────┐\x1b[0m\n"
    "\n◆ Python Environment\n"
    "  \x1b[32m✓\x1b[0m Python 3.13.5\n"
    "  ✓ SQLite 3.53.4\n"
    "    → state.db: WAL journal mode (15.4 MB)\n"
    "\n◆ API Connectivity\n"
    "  Running 40 connectivity checks in parallel…\r                                  \r  ⚠ OpenRouter API (not configured)\n"
    "  ⚠ Nous Portal auth (not logged in)\n"
    "  ✗ ~/.local/bin/hermes not found\n"
    "  ✓ ~/./config.yaml exists\n"
    "\nFound 2 issue(s) to address:\n").encode()


@pytest.mark.asyncio
async def test_doctor_requires_auth(monkeypatch):
    launches = fake_subprocess(monkeypatch, FakeProc())
    async with TestClient(TestServer(app())) as client:
        assert (await client.post("/vesper/doctor", json={})).status == 401
        assert (await client.post("/vesper/doctor", json={}, headers={"Authorization": "Bearer nope"})).status == 401
    assert launches == []


@pytest.mark.asyncio
async def test_doctor_runs_without_fix_and_cleans_the_report(monkeypatch):
    launches = fake_subprocess(monkeypatch, FakeProc(DOCTOR_OUTPUT, returncode=0))
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/doctor", headers=AUTH, json={})
        assert resp.status == 200
        data = await resp.json()

    argv, kwargs = launches[0]
    assert argv == [sys.executable, "-m", "hermes_cli.main", "doctor"]
    assert "--fix" not in argv and "--live" not in argv
    assert kwargs["env"]["NO_COLOR"] == "1" and kwargs["env"]["TERM"] == "dumb"
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT

    assert set(data) == {"ok", "exit_code", "output", "duration_ms", "summary"}
    assert data["ok"] is True and data["exit_code"] == 0
    assert isinstance(data["duration_ms"], int) and data["duration_ms"] >= 0
    assert "\x1b" not in data["output"] and "\r" not in data["output"]
    assert "Running 40 connectivity checks" not in data["output"]   # the progress redraw collapsed away
    assert "  ⚠ OpenRouter API (not configured)" in data["output"].splitlines()
    assert "  ✓ Python 3.13.5" in data["output"].splitlines()
    assert data["summary"] == {"ok": 3, "warnings": 2, "errors": 1}


@pytest.mark.asyncio
async def test_doctor_failure_exit_code_is_reported(monkeypatch):
    fake_subprocess(monkeypatch, FakeProc(b"  \xe2\x9c\x97 broken\n", returncode=1))
    async with TestClient(TestServer(app())) as client:
        data = await (await client.post("/vesper/doctor", headers=AUTH, json={"live": False})).json()
    assert data["ok"] is False and data["exit_code"] == 1
    assert data["summary"] == {"ok": 0, "warnings": 0, "errors": 1}


@pytest.mark.asyncio
async def test_live_flag_is_passed_only_when_asked_and_fix_never(monkeypatch):
    launches = fake_subprocess(monkeypatch, FakeProc(), FakeProc(), FakeProc())
    async with TestClient(TestServer(app())) as client:
        assert (await client.post("/vesper/doctor", headers=AUTH, json={"live": True, "fix": True})).status == 200
        assert (await client.post("/vesper/doctor", headers=AUTH)).status == 200   # no body at all
        assert (await client.post("/vesper/doctor", headers=AUTH, json={"live": False})).status == 200
    live_run, bare_run, off_run = (argv for argv, _ in launches)
    assert live_run == [sys.executable, "-m", "hermes_cli.main", "doctor", "--live"]
    assert bare_run == off_run == [sys.executable, "-m", "hermes_cli.main", "doctor"]
    assert all("--fix" not in argv for argv, _ in launches)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"live": "yes"}, {"live": 1}, ["live"]])
async def test_bad_doctor_bodies_are_400s(monkeypatch, body):
    launches = fake_subprocess(monkeypatch, FakeProc())
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/doctor", headers=AUTH, json=body)
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_request"
        resp = await client.post("/vesper/doctor", headers=AUTH, data="{oops")
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_json"
    assert launches == []


@pytest.mark.asyncio
async def test_doctor_timeout_kills_the_process_and_frees_the_lock(monkeypatch):
    monkeypatch.setattr(api_server_setup, "DOCTOR_TIMEOUT_SECONDS", 0.05)
    hung = FakeProc(hang=True)
    fake_subprocess(monkeypatch, hung, FakeProc(b"  \xe2\x9c\x93 fine\n"))
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/doctor", headers=AUTH, json={})
        assert resp.status == 504
        assert (await resp.json())["error"]["code"] == "doctor_timeout"
        assert hung.killed is True
        # A hung run must not wedge the endpoint.
        resp = await client.post("/vesper/doctor", headers=AUTH, json={})
        assert resp.status == 200 and (await resp.json())["summary"]["ok"] == 1


@pytest.mark.asyncio
async def test_concurrent_doctor_run_gets_409(monkeypatch):
    release = asyncio.Event()
    slow = FakeProc(b"  \xe2\x9c\x93 done\n", hold=release)
    launches = fake_subprocess(monkeypatch, slow, FakeProc(b"  \xe2\x9c\x93 again\n"))
    async with TestClient(TestServer(app())) as client:
        first = asyncio.ensure_future(client.post("/vesper/doctor", headers=AUTH, json={}))
        await asyncio.wait_for(slow.started.wait(), 5)

        busy = await client.post("/vesper/doctor", headers=AUTH, json={"live": True})
        assert busy.status == 409
        assert (await busy.json())["error"]["code"] == "doctor_busy"
        assert len(launches) == 1   # the rejected call never started a process

        release.set()
        resp = await asyncio.wait_for(first, 5)
        assert resp.status == 200
        assert (await client.post("/vesper/doctor", headers=AUTH, json={})).status == 200   # free again


@pytest.mark.asyncio
async def test_doctor_that_cannot_start_is_a_500(monkeypatch):
    fake_subprocess(monkeypatch, error=FileNotFoundError("python gone"))
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/doctor", headers=AUTH, json={})
        assert resp.status == 500 and (await resp.json())["error"]["code"] == "doctor_failed"
        # ... and the lock was released.
        assert (await client.post("/vesper/doctor", headers=AUTH, json={})).status == 500


SECRET_REPORT = "\n".join([
    f"  ✓ OpenRouter key {SK_KEY} accepted",
    "  ✓ OPENROUTER_API_KEY=plain-secret-value-123",
    "  ⚠ api_key: hunter2hunter2",
    "  ⚠ Authorization: Bearer abcDEF123456.tokenpart-xyz",
    '  ✗ DISCORD_BOT_TOKEN="quoted secret"',
    "  ✓ github token: ghp_notAPrefixWeKnow1234",
    f"    → state hash {HEX_RUN}",
    f"    → blob {B64_RUN}",
    "  ✓ ~/./config.yaml exists",
    "  ✓ Python 3.13.5",
    "  ✓ /opt/data/cron/executions is writable",
    "  ✓ Version files consistent (0.21.0)",
    "  ⚠ No GITHUB_TOKEN (60 req/hr rate limit)"])


@pytest.mark.asyncio
async def test_doctor_output_is_redacted_before_it_leaves(monkeypatch):
    fake_subprocess(monkeypatch, FakeProc(SECRET_REPORT.encode()))
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/doctor", headers=AUTH, json={})
        body = await resp.text()
        data = await resp.json()
    for secret in (SK_KEY, "0123456789abcdef0123", "plain-secret-value-123", "hunter2hunter2",
                   "abcDEF123456", "tokenpart-xyz", "quoted secret", "ghp_notAPrefixWeKnow1234", HEX_RUN, B64_RUN):
        assert secret not in body, secret
    out = data["output"].splitlines()
    assert out[:6] == [
        "  ✓ OpenRouter key [redacted] accepted",
        "  ✓ OPENROUTER_API_KEY=[redacted]",
        "  ⚠ api_key: [redacted]",
        "  ⚠ Authorization: Bearer [redacted]",
        "  ✗ DISCORD_BOT_TOKEN=[redacted]",
        "  ✓ github token: [redacted]"]
    assert out[6:8] == ["    → state hash [redacted]", "    → blob [redacted]"]
    # Ordinary diagnostics stay readable.
    assert out[8:] == [
        "  ✓ ~/./config.yaml exists", "  ✓ Python 3.13.5", "  ✓ /opt/data/cron/executions is writable",
        "  ✓ Version files consistent (0.21.0)", "  ⚠ No GITHUB_TOKEN (60 req/hr rate limit)"]
    assert data["summary"] == {"ok": 7, "warnings": 3, "errors": 1}


def test_redact_secrets_directly():
    redact = api_server_setup.redact_secrets
    assert redact(f"key {SK_KEY}!") == "key [redacted]!"
    assert redact("sk-proj-abcdefgh12345678") == "[redacted]"
    assert redact("Authorization: Bearer abc.def-ghi_jkl1234") == "Authorization: Bearer [redacted]"
    assert redact('token="two words"') == "token=[redacted]"
    assert redact("secret: 'a b' next") == "secret: [redacted] next"
    assert redact("password=hunter2") == "password=[redacted]"
    assert redact("client_secret : topsecret") == "client_secret : [redacted]"
    # Not secrets: short words, version strings, sentences mentioning "key", plain lowercase paths and rules.
    for text in ("task-queue sk-learn", "✓ API key or custom endpoint configured", "TERM=dumb", "NO_COLOR=1",
                 "/opt/hermes/web/node_modules/package", "-" * 40, "=" * 30, "Python 3.13.5 on linux"):
        assert redact(text) == text, text
    assert redact("sha " + "a1b2c3d4" * 4) == "sha [redacted]"
    assert redact("abcdefghijklmnopqrstuvwxyz") == "abcdefghijklmnopqrstuvwxyz"   # no digits, one case: a word
    assert redact("AbcdefghijklmnopqrstuvwxYz") == "[redacted]"


def test_clean_output_and_summary_directly():
    clean = api_server_setup.clean_doctor_output
    assert clean("\x1b[1m\x1b[32m✓\x1b[0m ok\r\n\x1b]0;title\x07  ⚠ careful\n\n") == "✓ ok\n  ⚠ careful"
    assert clean("progress 10%\rprogress 100%\r\n  ✓ done") == "progress 100%\n  ✓ done"
    assert clean("") == ""
    assert clean("x" * 10) == "x" * 10
    big = clean("line\n" * 100_000)
    assert big.endswith("[output truncated]") and len(big) < 250_000

    summary = api_server_setup.doctor_summary
    assert summary("  ✓ a\n    → ✓ not a check\n  ✓ b\n  ⚠ c\n  ✗ d\n  ✗ e\nplain ✓") == {
        "ok": 2, "warnings": 1, "errors": 2}
    assert summary("") == {"ok": 0, "warnings": 0, "errors": 0}
