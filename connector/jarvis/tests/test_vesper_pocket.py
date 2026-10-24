"""The dashboard's fast NAS voice (Pocket TTS on /vesper/tts): the real engine and worker protocol, driven by a
stand-in worker so no model is needed. Each persona's catalog voice, the config's own choice, the disk cache, and
cloning the persona's reference clip once Kyutai's gated weights are in HERMES_HOME."""

import asyncio
import json
import sys
import textwrap

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tools.tts_tool as tts_tool
from gateway.config import PlatformConfig
from jarvis import vesper_voice as voice
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}

FAKE_WORKER = textwrap.dedent('''
    import json, os, sys, wave
    settings = json.loads(sys.argv[1])
    log = os.environ["FAKE_POCKET_LOG"]
    print(json.dumps({"ready": True, "load_seconds": 0.1, "cloning": bool(settings.get("cloning_weights"))}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        open(log, "a").write(json.dumps({"text": req["text"], "voice": req["voice"], "hf_home": os.environ.get("HF_HOME"),
                                         "offline": os.environ.get("HF_HUB_OFFLINE")}) + "\\n")
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\\0\\0" * 2400)
        print(json.dumps({"id": req["id"], "ok": True, "seconds": 0.1, "took": 0.01}), flush=True)
''')


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def pocket(tmp_path, monkeypatch):
    worker = tmp_path / "fake_pocket.py"
    worker.write_text(FAKE_WORKER)
    (tmp_path / "signal.py").write_text("raise ImportError('shadowed the standard library')\n")   # see the OmniVoice tests
    log = tmp_path / "pocket.log"
    monkeypatch.setenv("FAKE_POCKET_LOG", str(log))
    monkeypatch.setattr(voice, "OMNIVOICE_PYTHON", type(voice.OMNIVOICE_PYTHON)(sys.executable))
    monkeypatch.setattr(voice, "POCKET_WORKER", worker)
    monkeypatch.setattr(voice, "POCKET_ENGINE", None)
    monkeypatch.setattr(voice, "available", lambda: {"elevenlabs": False, "pocket": True, "omnivoice": False, "edge": False, "piper": True})
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"pocket": {"voices": {"cortana": "caro_davy", "ultron": "not-a-voice"}}})
    return lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    application.on_cleanup.append(lambda _app: voice.shutdown())
    return application


@pytest.mark.asyncio
async def test_each_persona_speaks_its_catalog_voice_offline_and_repeats_come_from_the_cache(pocket):
    async with TestClient(TestServer(app())) as client:
        for persona in ("jarvis", "ultron", "cortana", "jarvis"):
            resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Good evening.", "persona": persona, "engine": "pocket"})
            assert resp.status == 200 and resp.headers["X-Vesper-Voice"] == "pocket" and (await resp.read())[:4] == b"RIFF"
    calls = pocket()
    # config.yaml's tts.pocket.voices picks Cortana's; a name that isn't in the catalog keeps the default
    assert [c["voice"] for c in calls] == [voice.POCKET_DEFAULTS["voices"]["jarvis"], voice.POCKET_DEFAULTS["voices"]["ultron"], "caro_davy"]
    assert all(c["offline"] == "1" and c["hf_home"] == str(voice.POCKET_HF) for c in calls)


@pytest.mark.asyncio
async def test_with_the_cloning_weights_it_speaks_in_the_personas_reference_clip(pocket, hermes_home):
    (hermes_home / "pocket-tts").mkdir()
    (hermes_home / "pocket-tts" / "model.safetensors").write_bytes(b"weights")
    async with TestClient(TestServer(app())) as client:
        st = await (await client.get("/vesper/voice/status?engine=pocket", headers=AUTH)).json()
        assert st["engine"] == "pocket"
        for _ in range(100):   # the status call woke the worker
            if voice.POCKET_ENGINE.running:
                break
            await asyncio.sleep(0.05)
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Good evening.", "persona": "ultron", "engine": "pocket"})
        assert resp.status == 200
        assert (await (await client.get("/vesper/voice/status?engine=pocket", headers=AUTH)).json())["cloning"] is True
    assert pocket()[-1]["voice"] == str(voice.SHIPPED_VOICES / "ultron.wav")


@pytest.mark.asyncio
async def test_the_voice_pack_speaks_without_the_cloning_weights(pocket, hermes_home):
    pack = hermes_home / "vesper-voices" / "pack"
    pack.mkdir(parents=True)
    for persona in ("jarvis", "ultron", "cortana"):
        (pack / f"{persona}.safetensors").write_bytes(b"state " + persona.encode())
    async with TestClient(TestServer(app())) as client:
        for persona in ("jarvis", "cortana"):
            resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Good evening.", "persona": persona, "engine": "pocket"})
            assert resp.status == 200 and resp.headers["X-Vesper-Voice"] == "pocket"
        st = await (await client.get("/vesper/voice/status?engine=pocket", headers=AUTH)).json()
        assert st["cloning"] is False and st["voices"] == {"jarvis": "pack", "ultron": "pack", "cortana": "pack"}
        setup = await (await client.get("/vesper/setup/voice", headers=AUTH)).json()
        assert setup["pocket"]["voice_pack"] is True
    assert [c["voice"] for c in pocket()] == [str(pack / "jarvis.safetensors"), str(pack / "cortana.safetensors")]


@pytest.mark.asyncio
async def test_your_own_clip_beats_the_pack_when_it_can_clone(pocket, hermes_home):
    (hermes_home / "pocket-tts").mkdir()
    (hermes_home / "pocket-tts" / "model.safetensors").write_bytes(b"weights")
    (hermes_home / "vesper-voices" / "pack").mkdir(parents=True)
    (hermes_home / "vesper-voices" / "pack" / "jarvis.safetensors").write_bytes(b"state")
    (hermes_home / "vesper-voices" / "pack" / "ultron.safetensors").write_bytes(b"state")
    (hermes_home / "vesper-voices" / "jarvis.wav").write_bytes(b"RIFF my own clip")
    engine = voice.pocket_engine()
    assert engine.pick_voice("jarvis", cloning=True) == ("clip", hermes_home / "vesper-voices" / "jarvis.wav")
    assert engine.pick_voice("ultron", cloning=True) == ("pack", hermes_home / "vesper-voices" / "pack" / "ultron.safetensors")
    assert engine.pick_voice("cortana", cloning=True) == ("clip", voice.SHIPPED_VOICES / "cortana.wav")
    assert engine.pick_voice("jarvis", cloning=False) == ("pack", hermes_home / "vesper-voices" / "pack" / "jarvis.safetensors")
    assert engine.pick_voice("cortana", cloning=False) == ("catalog", "caro_davy")
    assert engine.pick_voice("nobody", cloning=False)[0] == "pack"   # unknown personas speak as JARVIS
