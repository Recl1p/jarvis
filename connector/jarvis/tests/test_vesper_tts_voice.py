"""The dashboard's voice engines on /vesper/tts (api_server_vesper_voice.py): the engine picked under VOICE speaks
with the persona's voice, a failing one hands over to the next, and the request can only pick voices. The online
providers are stand-ins; the NAS models are covered with stand-in workers in test_vesper_omnivoice.py and
test_vesper_pocket.py."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tools.tts_tool as tts_tool
import tools.tts_tool_local as local_tts
import tools.tts_tool_providers as providers
from gateway.config import PlatformConfig
from jarvis import vesper_voice as voice
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
JARVIS_EL = "VGPRBzHikUaaOd5ObCEz"


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


@pytest.fixture
def engines(monkeypatch):
    """Every engine set up, the NAS ones as stand-ins: `log` records each attempt, `fail` makes an engine (or one
    ElevenLabs voice id) fail."""
    log = {"elevenlabs": [], "edge": [], "piper": [], "pocket": [], "fail": set()}
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "elevenlabs", "elevenlabs": {"voice_id": "UsersOwnVoice0000000"}})
    monkeypatch.setattr(voice, "available", lambda: {"elevenlabs": True, "pocket": True, "omnivoice": False, "edge": True, "piper": True})

    def elevenlabs(text, path, cfg):
        vid = cfg["elevenlabs"]["voice_id"]
        log["elevenlabs"].append(vid)
        if vid in log["fail"] or "elevenlabs" in log["fail"]:
            raise RuntimeError("quota_exceeded")
        open(path, "wb").write(b"EL:" + vid.encode() + b" " * 64)

    async def edge(text, path, cfg):
        log["edge"].append((cfg.get("edge") or {}).get("voice"))
        if "edge" in log["fail"]:
            raise OSError("speech.platform.bing.com unreachable")
        open(path, "wb").write(b"EDGE" + b" " * 64)

    def piper(text, path, cfg):
        log["piper"].append((cfg.get("piper") or {}).get("voice"))
        open(path, "wb").write(b"PIPER")

    class Pocket:
        async def synthesize(self, text, persona, still_wanted=lambda: True, background=False):
            log["pocket"].append(persona)
            if "pocket" in log["fail"]:
                raise RuntimeError("worker exited")
            return b"POCKET:" + persona.encode()

    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", elevenlabs)
    monkeypatch.setattr(providers, "_generate_edge_tts", edge)
    monkeypatch.setattr(local_tts, "_generate_piper_tts", piper)
    monkeypatch.setattr(voice, "POCKET_ENGINE", Pocket())
    return log


async def say(client, **body):
    resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "On it.", "persona": "jarvis", **body})
    return resp.status, (await resp.read()).rstrip(), resp.headers.get("X-Vesper-Voice")


@pytest.mark.asyncio
async def test_elevenlabs_speaks_in_the_personas_own_voice_and_repeats_cost_nothing(engines):
    async with TestClient(TestServer(app())) as client:
        assert await say(client, engine="elevenlabs", voice_id=JARVIS_EL) == (200, b"EL:" + JARVIS_EL.encode(), "elevenlabs")
        assert await say(client, engine="elevenlabs", voice_id=JARVIS_EL) == (200, b"EL:" + JARVIS_EL.encode(), "elevenlabs")
        assert (await say(client, engine="elevenlabs"))[1] == b"EL:UsersOwnVoice0000000"   # no persona voice: the configured one
    assert engines["elevenlabs"] == [JARVIS_EL, "UsersOwnVoice0000000"]   # the repeat came from the cache


@pytest.mark.asyncio
async def test_without_a_choice_the_default_is_elevenlabs_with_a_key_else_pocket(engines, monkeypatch):
    async with TestClient(TestServer(app())) as client:
        assert (await say(client, voice_id=JARVIS_EL))[2] == "elevenlabs"
        monkeypatch.setattr(voice, "available", lambda: {"elevenlabs": False, "pocket": True, "omnivoice": True, "edge": True, "piper": True})
        assert await say(client, text="Hello.", voice_id=JARVIS_EL) == (200, b"POCKET:jarvis", "pocket")


@pytest.mark.asyncio
async def test_a_failing_engine_hands_over_to_the_next_down_to_piper(engines):
    """ElevenLabs out of characters: Pocket TTS on the NAS. Pocket failing too: the persona's Piper voice."""
    engines["fail"].add("elevenlabs")
    async with TestClient(TestServer(app())) as client:
        assert await say(client, engine="elevenlabs", voice_id=JARVIS_EL, persona="ultron") == (200, b"POCKET:ultron", "pocket")
        engines["fail"].add("pocket")
        assert await say(client, engine="elevenlabs", voice_id=JARVIS_EL, piper_voice="en_US-ryan-medium") == (200, b"PIPER", "local")
        engines["fail"].add("edge")
        assert (await say(client, engine="edge", edge_voice="en-US-ChristopherNeural"))[2] == "local"
    assert engines["elevenlabs"][:2] == [JARVIS_EL, "UsersOwnVoice0000000"]   # its configured voice was tried first
    assert engines["piper"] == ["en_US-ryan-medium", "en_GB-alan-medium"]


@pytest.mark.asyncio
async def test_an_engine_that_isnt_set_up_is_skipped(engines, monkeypatch):
    monkeypatch.setattr(voice, "available", lambda: {"elevenlabs": False, "pocket": False, "omnivoice": False, "edge": True, "piper": True})
    async with TestClient(TestServer(app())) as client:
        assert (await say(client, engine="elevenlabs"))[2] == "local"   # no key, no Pocket: Piper, without trying
        assert await say(client, engine="edge", edge_voice="en-GB-RyanNeural") == (200, b"EDGE", "edge")
    assert engines["elevenlabs"] == [] and engines["pocket"] == [] and engines["edge"] == ["en-GB-RyanNeural"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"voice_id": "../../etc"}, {"voice_id": "a b"}, {"edge_voice": "http://evil"}, {"edge_voice": "en-US-Guy"},
                                  {"piper_voice": "../../etc/passwd"}, {"engine": "bark"}, {"persona": "../x"}])
async def test_bad_voice_values_are_rejected(engines, body):
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Hi.", **body})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_voice"
    assert engines["elevenlabs"] == [] and engines["pocket"] == []


@pytest.mark.asyncio
async def test_status_lists_the_engines_and_what_a_choice_resolves_to(engines, monkeypatch):
    async with TestClient(TestServer(app())) as client:
        st = await (await client.get("/vesper/voice/status", headers=AUTH)).json()
        assert st["engine"] == st["default"] == "elevenlabs"
        assert {e["id"] for e in st["engines"]} == set(voice.ENGINES) and all("label" in e and "available" in e for e in st["engines"])
        assert not next(e for e in st["engines"] if e["id"] == "omnivoice")["available"]
        st = await (await client.get("/vesper/voice/status?engine=edge", headers=AUTH)).json()
        assert st["engine"] == "edge" and st["default"] == "elevenlabs"
        prewarm = await client.post("/vesper/voice/prewarm", headers=AUTH, json={"persona": "jarvis", "texts": ["On it."], "engine": "elevenlabs"})
        assert (await prewarm.json())["queued"] == 0   # ElevenLabs lines aren't rendered ahead (they'd spend characters)
        assert (await client.get("/vesper/voice/status")).status == 401
    assert engines["elevenlabs"] == []


def test_the_key_check_reports_presence_only(monkeypatch):
    monkeypatch.setattr(providers, "_require_key", lambda *a: "sk-secret")
    assert voice.elevenlabs_ready() is True
    monkeypatch.setattr(providers, "_require_key", lambda *a: (_ for _ in ()).throw(ValueError("ELEVENLABS_API_KEY not set")))
    assert voice.elevenlabs_ready() is False
