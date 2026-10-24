"""The dashboard's self-hosted voice (/vesper/tts with OmniVoice): the real engine and worker protocol, driven by a
stand-in worker so no model is needed. Persona clips, the user's own clip, the disk cache, the Piper fallback,
prewarming, and OmniVoice switched off in config.yaml."""

import asyncio
import json
import os
import sys
import textwrap
import wave

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tools.tts_tool as tts_tool
import tools.tts_tool_local as local_tts
from gateway.config import PlatformConfig
from jarvis import vesper_voice as voice
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}

FAKE_WORKER = textwrap.dedent('''
    import json, os, subprocess, sys, wave
    log = os.environ["FAKE_OMNIVOICE_LOG"]
    print(json.dumps({"ready": True, "load_seconds": 0.1}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        open(log, "a").write(json.dumps({"text": req["text"], "ref_wav": req["ref_wav"], "settings": sys.argv[1], "steps": req.get("steps")}) + "\\n")
        if "FAIL" in req["text"]:
            print(json.dumps({"id": req["id"], "ok": False, "error": "boom"}), flush=True); continue
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
def omnivoice(tmp_path, monkeypatch):
    """An 'installed' OmniVoice whose worker is the stand-in above. Returns a reader for the worker's request log."""
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    # The real worker sits in gateway/platforms, next to a signal.py: run unisolated, that shadows the stdlib.
    (tmp_path / "signal.py").write_text("raise ImportError('shadowed the standard library')\n")
    model = tmp_path / "snapshot"
    model.mkdir()
    log = tmp_path / "worker.log"
    monkeypatch.setenv("FAKE_OMNIVOICE_LOG", str(log))
    monkeypatch.setattr(voice, "OMNIVOICE_PYTHON", type(voice.OMNIVOICE_PYTHON)(sys.executable))
    monkeypatch.setattr(voice, "WORKER", worker)
    monkeypatch.setattr(voice, "model_dir", lambda: model)
    monkeypatch.setattr(voice, "ENGINE", None)
    monkeypatch.setattr(voice, "pocket_installed", lambda: False)
    monkeypatch.setattr(voice, "elevenlabs_ready", lambda: False)   # the default engine is then OmniVoice
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "elevenlabs"})
    return lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)

    application.on_cleanup.append(lambda _app: voice.shutdown())
    return application


def test_every_persona_ships_a_usable_reference_clip():
    for persona in voice.PERSONAS:
        wav = voice.SHIPPED_VOICES / f"{persona}.wav"
        txt = voice.SHIPPED_VOICES / f"{persona}.txt"
        with wave.open(str(wav)) as w:
            seconds = w.getnframes() / w.getframerate()
        assert 3 <= seconds <= 10, (persona, seconds)   # OmniVoice wants a 3-10 s reference
        assert txt.read_text().strip()


@pytest.mark.asyncio
async def test_each_persona_speaks_in_its_own_cloned_voice_and_repeats_come_from_the_cache(omnivoice):
    async with TestClient(TestServer(app())) as client:
        for persona in ("jarvis", "ultron", "cortana"):
            resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "On it.", "persona": persona})
            assert resp.status == 200 and resp.headers["X-Vesper-Voice"] == "omnivoice" and resp.content_type == "audio/wav"
            assert (await resp.read())[:4] == b"RIFF"
        again = await client.post("/vesper/tts", headers=AUTH, json={"text": "On it.", "persona": "ultron"})
        assert again.status == 200 and again.headers["X-Vesper-Voice"] == "omnivoice"
    calls = omnivoice()
    assert [c["ref_wav"].rsplit("/", 1)[-1] for c in calls] == ["jarvis.wav", "ultron.wav", "cortana.wav"]   # the repeat never reached the worker
    assert json.loads(calls[0]["settings"])["steps"] == voice.DEFAULTS["steps"]
    assert calls[0]["steps"] == voice.CAREFUL["steps"]   # "On it." is a short line: rendered carefully


@pytest.mark.asyncio
async def test_a_clip_the_user_put_in_their_home_replaces_the_shipped_voice(omnivoice, hermes_home):
    own = hermes_home / "vesper-voices"
    own.mkdir()
    (own / "cortana.wav").write_bytes((voice.SHIPPED_VOICES / "jarvis.wav").read_bytes())
    (own / "cortana.txt").write_text("My own reference line.")
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Hello there.", "persona": "cortana"})
        assert resp.status == 200
    assert omnivoice()[-1]["ref_wav"] == str(own / "cortana.wav")


@pytest.mark.asyncio
async def test_omnivoice_failure_falls_back_to_the_personas_piper_voice(omnivoice, monkeypatch):
    used = []

    def piper(text, path, cfg):
        used.append(cfg["piper"]["voice"])
        open(path, "wb").write(b"RIFFpiper")
    monkeypatch.setattr(local_tts, "_generate_piper_tts", piper)
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "FAIL please", "persona": "ultron", "piper_voice": "en_US-ryan-medium"})
        assert resp.status == 200 and resp.headers["X-Vesper-Voice"] == "local" and await resp.read() == b"RIFFpiper"
        bad = await client.post("/vesper/tts", headers=AUTH, json={"text": "Hi.", "persona": "../../etc"})
        assert bad.status == 400 and (await bad.json())["error"]["code"] == "invalid_voice"
    assert used == ["en_US-ryan-medium"]


@pytest.mark.asyncio
async def test_prewarm_renders_fixed_lines_ahead_and_status_wakes_the_engine(omnivoice):
    async with TestClient(TestServer(app())) as client:
        status = await (await client.get("/vesper/voice/status", headers=AUTH)).json()
        assert status["engine"] == "omnivoice" and "running" in status
        resp = await client.post("/vesper/voice/prewarm", headers=AUTH, json={"persona": "jarvis", "texts": ["On it.", "Still working on it."]})
        assert (await resp.json())["queued"] == 2
        for _ in range(100):
            if len(omnivoice()) >= 2:
                break
            await asyncio.sleep(0.05)
        before = len(omnivoice())
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Still working on it.", "persona": "jarvis"})
        assert resp.status == 200 and len(omnivoice()) == before   # served from what prewarm rendered
        assert (await client.post("/vesper/voice/prewarm", headers=AUTH, json={"persona": "nobody", "texts": []})).status == 400
        assert (await client.get("/vesper/voice/status")).status == 401


@pytest.mark.asyncio
async def test_switched_off_in_config_omnivoice_is_not_offered_and_never_starts(omnivoice, monkeypatch):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "elevenlabs", "omnivoice": {"enabled": False}})
    monkeypatch.setattr(local_tts, "_generate_piper_tts", lambda text, path, cfg: open(path, "wb").write(b"PIPER"))
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/tts", headers=AUTH, json={"text": "Hi.", "persona": "jarvis", "engine": "omnivoice"})
        assert resp.status == 200 and resp.headers["X-Vesper-Voice"] == "local"
        st = await (await client.get("/vesper/voice/status?engine=omnivoice", headers=AUTH)).json()
        assert not next(e for e in st["engines"] if e["id"] == "omnivoice")["available"] and "running" not in st
    assert omnivoice() == []


def test_cache_is_kept_under_its_size_limit(hermes_home):
    folder = voice.cache_dir()
    folder.mkdir(parents=True)
    for i in range(5):
        (folder / f"{i}.wav").write_bytes(b"x" * 1000)
        os.utime(folder / f"{i}.wav", (1_000_000 + i, 1_000_000 + i))   # oldest first
    voice.prune_cache(limit=2500)
    assert sorted(p.name for p in folder.glob("*.wav")) == ["3.wav", "4.wav"]


def test_short_and_prewarmed_lines_get_the_careful_settings_long_ones_the_fast():
    cfg = dict(voice.DEFAULTS)
    assert voice.quality_for("On it.", cfg) == voice.CAREFUL
    assert voice.quality_for("Your next meeting starts in twenty minutes.", cfg) == {"steps": cfg["steps"], "guidance": cfg["guidance"]}
    assert voice.quality_for("Your next meeting starts in twenty minutes.", cfg, background=True) == voice.CAREFUL
    assert voice.quality_for("On it.", {**cfg, "steps": 32})["steps"] == 32   # a slower configured default is kept
