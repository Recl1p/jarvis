"""The setup guide's voice step: an ElevenLabs key with a voice per persona, and Pocket TTS's voice-cloning model
downloaded from Hugging Face. ElevenLabs and the Hub are local fake servers; HERMES_HOME is a temp dir."""

import asyncio
import hashlib

import pytest
import pytest_asyncio
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import setup_voice as setup
from jarvis import vesper_voice as voice
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
EL_KEY = "sk_" + "e1" * 24
EL_SPEECH_ONLY = "sk_" + "5p" * 24
HF_TOKEN = "hf_" + "Tk" * 17
HF_NO_TERMS = "hf_" + "Nt" * 17
WEIGHTS = b"pocket-cloning-weights " * 4096
FAKE_MP3 = b"ID3" + bytes(200)
ENV_VARS = ("ELEVENLABS_API_KEY", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ENV_VARS:   # save_env_value publishes into os.environ: register each so teardown removes it
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)
    monkeypatch.setattr(voice, "pocket_installed", lambda: True)
    monkeypatch.setattr(setup, "_download", {"state": "idle", "done": 0, "total": 0, "error": ""})
    return tmp_path


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def upstream():
    """ElevenLabs and the Hub on one fake server, the Hub's file storage on another host, as on huggingface.co.
    Records who asked what, with which auth."""
    seen = []
    voices = [{"voice_id": "premade0000000000001", "name": "Adam", "category": "premade"},
              {"voice_id": "clonedvoice000000001", "name": "My butler", "category": "cloned"}]

    async def el_voices(request):
        key = request.headers.get("xi-api-key")
        if key == EL_KEY:
            return web.json_response({"voices": voices})
        if key == EL_SPEECH_ONLY:
            return web.json_response({"detail": {"status": "missing_permissions"}}, status=401)
        return web.json_response({"detail": {"status": "invalid_api_key"}}, status=401)

    def hf_auth(request):
        return request.headers.get("Authorization", "")

    async def whoami(request):
        ok = hf_auth(request) in (f"Bearer {HF_TOKEN}", f"Bearer {HF_NO_TERMS}")
        return web.json_response({"name": "someone"} if ok else {"error": "Invalid credentials"}, status=200 if ok else 401)

    async def resolve(request):
        seen.append(("hub", request.method, hf_auth(request)))
        if hf_auth(request) != f"Bearer {HF_TOKEN}":
            return web.Response(status=403 if hf_auth(request) else 401, headers={"X-Error-Code": "GatedRepo"})
        raise web.HTTPFound(request.app["links"]["storage"] + "/storage/weights?signed=1")

    async def storage(request):
        seen.append(("storage", request.method, hf_auth(request)))
        return web.Response(body=request.app["state"]["weights"])

    application = web.Application()
    application.router.add_get("/v1/voices", el_voices)
    application.router.add_get("/api/whoami-v2", whoami)
    application.router.add_route("*", f"/{setup.POCKET_REPO}/resolve/{setup.POCKET_REVISION}/{setup.POCKET_FILE}", resolve)
    application["links"] = {}   # filled in once the storage host is up
    store = web.Application()
    store.router.add_get("/storage/weights", storage)
    store["state"] = {"weights": WEIGHTS}
    return application, store, seen


@pytest_asyncio.fixture
async def fake(monkeypatch):
    application, store, seen = upstream()
    async with TestServer(store, host="127.0.0.1") as storage, TestServer(application, host="127.0.0.1") as server:
        application["links"]["storage"] = str(storage.make_url("")).rstrip("/")
        base = str(server.make_url("")).rstrip("/")
        monkeypatch.setattr(setup, "ELEVENLABS_API", base)
        monkeypatch.setattr(setup, "HF_ENDPOINT", base)
        monkeypatch.setattr(setup, "POCKET_SIZE", len(WEIGHTS))
        monkeypatch.setattr(setup, "POCKET_SHA256", hashlib.sha256(WEIGHTS).hexdigest())
        yield store["state"], seen


def env_file(home):
    path = home / ".env"
    return path.read_text() if path.exists() else ""


async def wait_download(client):
    for _ in range(200):
        status = await (await client.get("/vesper/setup/voice", headers=AUTH)).json()
        if status["pocket"]["download"]["state"] != "downloading":
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("download never finished")


@pytest.mark.asyncio
async def test_every_voice_route_needs_the_dashboard_key(fake):
    async with TestClient(TestServer(app())) as client:
        for method, path in (("GET", "/vesper/setup/voice"), ("POST", "/vesper/setup/voice/elevenlabs"),
                             ("GET", "/vesper/setup/voice/elevenlabs/voices"), ("PUT", "/vesper/setup/voice/elevenlabs/voices"),
                             ("POST", "/vesper/setup/voice/pocket")):
            assert (await client.request(method, path, json={})).status == 401


@pytest.mark.asyncio
async def test_a_refused_elevenlabs_key_saves_nothing(fake, hermes_home):
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/voice/elevenlabs", headers=AUTH, json={"api_key": "sk_" + "no" * 24})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_key"
        status = await (await client.get("/vesper/setup/voice", headers=AUTH)).json()
    assert "ELEVENLABS_API_KEY" not in env_file(hermes_home)
    assert status["elevenlabs"]["connected"] is False and status["engines"]["elevenlabs"] is False


@pytest.mark.asyncio
async def test_an_elevenlabs_key_is_saved_and_its_own_voices_come_first(fake, hermes_home):
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/voice/elevenlabs", headers=AUTH, json={"api_key": EL_KEY})
        body = await resp.json()
        status = await (await client.get("/vesper/setup/voice", headers=AUTH)).json()
    assert resp.status == 200 and body["ok"] is True
    assert EL_KEY not in str(body) and EL_KEY in env_file(hermes_home)
    assert [v["category"] for v in body["voices"]] == ["cloned", "premade"]
    assert status["elevenlabs"]["connected"] is True and status["default"] == "elevenlabs"


@pytest.mark.asyncio
async def test_a_speech_only_key_is_accepted_without_a_voice_list(fake, hermes_home):
    async with TestClient(TestServer(app())) as client:
        body = await (await client.post("/vesper/setup/voice/elevenlabs", headers=AUTH, json={"api_key": EL_SPEECH_ONLY})).json()
    assert body["ok"] is True and body["voices"] is None and body["voices_note"]
    assert EL_SPEECH_ONLY in env_file(hermes_home)


@pytest.mark.asyncio
async def test_the_voice_picked_for_a_persona_is_spoken_before_its_personas_js_voice(fake, hermes_home, monkeypatch):
    spoken = []

    def fake_generate(text, path, cfg):
        spoken.append(cfg["elevenlabs"].get("voice_id"))
        if cfg["elevenlabs"].get("voice_id") != "clonedvoice000000001":
            raise RuntimeError("voice_not_found")
        with open(path, "wb") as out:
            out.write(FAKE_MP3)
        return path

    from tools import tts_tool
    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", fake_generate)
    async with TestClient(TestServer(app())) as client:
        await client.post("/vesper/setup/voice/elevenlabs", headers=AUTH, json={"api_key": EL_KEY})
        assert (await client.put("/vesper/setup/voice/elevenlabs/voices", headers=AUTH,
                                 json={"voices": {"cortana": "../etc"}})).status == 400
        resp = await client.put("/vesper/setup/voice/elevenlabs/voices", headers=AUTH,
                                json={"voices": {"jarvis": "clonedvoice000000001", "ultron": None}})
        assert (await resp.json())["persona_voices"] == {"jarvis": "clonedvoice000000001"}
        listed = await (await client.get("/vesper/setup/voice/elevenlabs/voices", headers=AUTH)).json()
    assert listed["persona_voices"] == {"jarvis": "clonedvoice000000001"}
    cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert cfg["tts"]["elevenlabs"]["persona_voices"] == {"jarvis": "clonedvoice000000001"}
    # personas.js sends the owner's own voice id, which this account doesn't have
    audio, _ = await voice._elevenlabs("Good evening.", "jarvis", {"voice_id": "OwnersVoice000000001"}, lambda: True)
    assert audio == FAKE_MP3 and spoken == ["clonedvoice000000001"]
    spoken.clear()
    with pytest.raises(RuntimeError):   # no pick for Cortana: the personas.js voice is tried first, as before
        await voice._elevenlabs("Hello.", "cortana", {"voice_id": "OwnersVoice000000002"}, lambda: True)
    assert spoken[0] == "OwnersVoice000000002"


@pytest.mark.asyncio
async def test_a_bad_hugging_face_token_or_unaccepted_terms_download_nothing(fake, hermes_home):
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={})
        assert (await resp.json())["error"]["code"] == "missing_token"
        resp = await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={"hf_token": "hf_" + "Zz" * 17})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_token"
        resp = await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={"hf_token": HF_NO_TERMS})
        assert resp.status == 400 and (await resp.json())["error"]["code"] == "no_access"
    assert "HF_TOKEN" not in env_file(hermes_home)
    assert not (hermes_home / "pocket-tts").exists()


@pytest.mark.asyncio
async def test_the_cloning_model_downloads_verified_and_the_token_stays_on_the_hub(fake, hermes_home):
    _state, seen = fake
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={"hf_token": HF_TOKEN})
        assert resp.status == 200 and HF_TOKEN not in await resp.text()
        status = await wait_download(client)
        again = await (await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={})).json()
    assert status["pocket"]["cloning"] is True and status["pocket"]["download"]["error"] == ""
    assert (hermes_home / "pocket-tts" / "model.safetensors").read_bytes() == WEIGHTS
    assert voice.pocket_cloning_weights() == hermes_home / "pocket-tts" / "model.safetensors"
    assert f"HF_TOKEN={HF_TOKEN}" in env_file(hermes_home) and status["huggingface"]["token_saved"] is True
    assert ("storage", "GET", "") in seen and not any(where == "storage" and auth for where, _m, auth in seen)
    assert again["pocket"]["cloning"] is True   # already there: nothing downloads twice


@pytest.mark.asyncio
async def test_a_download_that_does_not_match_the_published_file_is_not_installed(fake, hermes_home):
    state, _seen = fake
    state["weights"] = WEIGHTS[:-1] + b"!"
    async with TestClient(TestServer(app())) as client:
        await client.post("/vesper/setup/voice/pocket", headers=AUTH, json={"hf_token": HF_TOKEN})
        status = await wait_download(client)
    assert status["pocket"]["download"]["state"] == "error" and "didn't match" in status["pocket"]["download"]["error"]
    assert status["pocket"]["cloning"] is False
    assert not any((hermes_home / "pocket-tts").iterdir())
