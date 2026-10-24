"""Voice setup for the dashboard guide (its Voice step): ElevenLabs, and the voice-cloning model that lets Pocket TTS
speak in the persona's own voice. No terminal needed.

- ElevenLabs: a pasted key is checked against the account (GET /v1/voices) and saved as ELEVENLABS_API_KEY. The
  account's voices are then listed so each persona can have one of them, saved as config.yaml
  ``tts.elevenlabs.persona_voices``. api_server_vesper_voice tries that before the persona's id in personas.js: those
  ids are the owner's own cloned voices and don't exist in anyone else's account.
- Pocket TTS speaks out of the box with its catalog voices. To clone the persona's reference clip it needs Kyutai's
  gated weights (huggingface.co/kyutai/pocket-tts). The user accepts the terms there with a free account and pastes a
  read token. The guide checks the token and the access, saves the token as HF_TOKEN, downloads the pinned file to
  $HERMES_HOME/pocket-tts/model.safetensors (checked against its size and SHA-256), then stops the Pocket worker so
  the next line it speaks is cloned.

Nothing here logs or returns a key or a token.
"""

import asyncio
import hashlib
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from functools import partial
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

ELEVENLABS_API = "https://api.elevenlabs.io"
ELEVENLABS_KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"
HF_ENDPOINT = "https://huggingface.co"
POCKET_REPO = "kyutai/pocket-tts"
# The revision and file english.yaml names (HANDOFF 32); size from the Hub listing, SHA-256 of the copy installed on
# the NAS from that revision (the Hub hides the hash of a gated file until access is granted).
POCKET_REVISION = "983151f13aaeab1b13c1e5e3c2c383d49a9edf3f"
POCKET_FILE = "languages/english/model.safetensors"
POCKET_SIZE = 219_029_196
POCKET_SHA256 = "fb0dc01b0d4d2e1c905b7a3e0676e3d9c96d5ae460e24e3ab94981805babf997"
POCKET_TERMS_URL = f"https://huggingface.co/{POCKET_REPO}"
HF_TOKENS_URL = "https://huggingface.co/settings/tokens"
CHECK_TIMEOUT = 15
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,400}")   # one printable token, no spaces
_VOICE_ID = re.compile(r"[A-Za-z0-9]{8,40}")
# The account's own voices first (the ones a user made for a persona), then ElevenLabs' library.
_CATEGORY_ORDER = {"cloned": 0, "professional": 1, "generated": 2, "premade": 3}

_lock = threading.Lock()
_download = {"state": "idle", "done": 0, "total": 0, "error": ""}   # idle | downloading | error


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def http_routes(adapter):
    return [
        ("GET", "/vesper/setup/voice", partial(setup_status, adapter)),
        ("POST", "/vesper/setup/voice/elevenlabs", partial(connect_elevenlabs, adapter)),
        ("GET", "/vesper/setup/voice/elevenlabs/voices", partial(list_elevenlabs_voices, adapter)),
        ("PUT", "/vesper/setup/voice/elevenlabs/voices", partial(save_persona_voices, adapter)),
        ("POST", "/vesper/setup/voice/pocket", partial(start_pocket_cloning, adapter)),
    ]


async def _json_body(request):
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return None, _error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return None, _error(400, "invalid_request", "Expected a JSON object")
    return body, None


def _saved(name):
    from hermes_cli.config import get_env_value_prefer_dotenv
    return (get_env_value_prefer_dotenv(name) or "").strip()


# ---- Status -----------------------------------------------------------------------------------

def persona_voices() -> dict:
    """config.yaml tts.elevenlabs.persona_voices, valid entries only."""
    from .vesper_voice import PERSONAS, _tts_config
    raw = (_tts_config().get("elevenlabs") or {}).get("persona_voices")
    raw = raw if isinstance(raw, dict) else {}
    return {p: raw[p] for p in PERSONAS if isinstance(raw.get(p), str) and _VOICE_ID.fullmatch(raw[p])}


def _status_payload():
    from . import vesper_voice as voice
    avail = voice.available()
    with _lock:
        download = dict(_download)
    return {
        "engines": {k: bool(v) for k, v in avail.items()}, "default": voice.resolve(None, avail),
        "elevenlabs": {"connected": bool(avail.get("elevenlabs")), "keys_url": ELEVENLABS_KEYS_URL,
                       "persona_voices": persona_voices()},
        "pocket": {"installed": voice.pocket_installed(), "cloning": voice.pocket_cloning_weights() is not None,
                   "voice_pack": voice.pack_installed(),
                   "download": download, "size": POCKET_SIZE, "terms_url": POCKET_TERMS_URL, "tokens_url": HF_TOKENS_URL},
        "huggingface": {"token_saved": bool(_saved("HF_TOKEN") or _saved("HUGGING_FACE_HUB_TOKEN"))},
    }


async def setup_status(adapter, request):
    """GET /vesper/setup/voice -- which voices are set up, and how the cloning download is going."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response(await asyncio.to_thread(_status_payload))


# ---- ElevenLabs -------------------------------------------------------------------------------

async def _elevenlabs_voices(key):
    """(status, [voices] or None). status: ok | rejected | no_voice_access (a key restricted to speech) | unreachable."""
    import aiohttp as _aiohttp_client
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get(f"{ELEVENLABS_API}/v1/voices", headers={"xi-api-key": key},
                                   timeout=_aiohttp_client.ClientTimeout(total=CHECK_TIMEOUT)) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                if resp.status == 200 and isinstance(data, dict):
                    return "ok", _voice_rows(data.get("voices"))
                detail = data.get("detail") if isinstance(data, dict) else None
                reason = detail.get("status") if isinstance(detail, dict) else ""
                if resp.status in (401, 403):
                    return ("no_voice_access" if reason == "missing_permissions" else "rejected"), None
                return "unreachable", None
    except Exception as exc:
        logger.warning("ElevenLabs voice list failed: %s", type(exc).__name__)
        return "unreachable", None


def _voice_rows(voices):
    rows = [{"id": v["voice_id"], "name": str(v.get("name") or v["voice_id"])[:80], "category": str(v.get("category") or "")}
            for v in (voices if isinstance(voices, list) else [])
            if isinstance(v, dict) and isinstance(v.get("voice_id"), str) and _VOICE_ID.fullmatch(v["voice_id"])]
    rows.sort(key=lambda r: (_CATEGORY_ORDER.get(r["category"], 9), r["name"].lower()))
    return rows[:300]


def _save_elevenlabs_key(key):
    from hermes_cli.config import save_env_value
    save_env_value("ELEVENLABS_API_KEY", key)
    return None if _saved("ELEVENLABS_API_KEY") == key else "Hermes could not save the key (the configuration is locked)"


async def connect_elevenlabs(adapter, request):
    """POST /vesper/setup/voice/elevenlabs {api_key} -- check and save an ElevenLabs key; returns the account's voices."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    key = body.get("api_key").strip() if isinstance(body.get("api_key"), str) else ""
    if not key:
        return _error(400, "missing_key", "Paste the ElevenLabs API key to continue")
    if not _KEY_SHAPE.fullmatch(key):
        return _error(400, "invalid_key", "That doesn't look like an API key (it should be one word, no spaces)")
    status, voices = await _elevenlabs_voices(key)
    if status == "rejected":
        return _error(400, "invalid_key", "ElevenLabs rejected that key")
    problem = await asyncio.to_thread(_save_elevenlabs_key, key)
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "voices": voices, "voices_note": {
        "no_voice_access": "This key can speak but can't list voices, so each persona uses ElevenLabs' default voice.",
        "unreachable": "The key is saved, but ElevenLabs didn't answer, so its voices couldn't be listed yet."}.get(status, ""),
        "persona_voices": await asyncio.to_thread(persona_voices)})


async def list_elevenlabs_voices(adapter, request):
    """GET /vesper/setup/voice/elevenlabs/voices -- the saved key's voices and the one each persona uses."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    key = await asyncio.to_thread(_saved, "ELEVENLABS_API_KEY")
    if not key:
        return _error(409, "not_connected", "ElevenLabs isn't connected yet")
    status, voices = await _elevenlabs_voices(key)
    if status == "rejected":
        return _error(400, "invalid_key", "ElevenLabs no longer accepts the saved key. Paste a new one.")
    return web.json_response({"voices": voices, "persona_voices": await asyncio.to_thread(persona_voices),
                              "unavailable": status != "ok"})


def _write_persona_voices(chosen):
    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    el = cfg.setdefault("tts", {}).setdefault("elevenlabs", {})
    if not isinstance(el, dict):
        cfg["tts"]["elevenlabs"] = el = {}
    voices = {**(el.get("persona_voices") if isinstance(el.get("persona_voices"), dict) else {}), **chosen}
    voices = {p: v for p, v in voices.items() if v}
    if voices:
        el["persona_voices"] = voices
    else:
        el.pop("persona_voices", None)
    save_config(cfg)
    return persona_voices()


async def save_persona_voices(adapter, request):
    """PUT /vesper/setup/voice/elevenlabs/voices {voices: {persona: voice id | null}} -- null goes back to the persona's
    own voice from personas.js."""
    from .vesper_voice import PERSONAS
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    chosen = body.get("voices")
    if (not isinstance(chosen, dict) or not chosen or any(p not in PERSONAS for p in chosen)
            or any(v is not None and not (isinstance(v, str) and _VOICE_ID.fullmatch(v)) for v in chosen.values())):
        return _error(400, "invalid_voices", "Expected {voices: {persona: voice id or null}}")
    try:
        saved = await asyncio.to_thread(_write_persona_voices, chosen)
    except Exception as exc:
        logger.warning("persona voices could not be saved: %s", type(exc).__name__)
        return _error(500, "save_failed", "Hermes could not save the voices")
    return web.json_response({"ok": True, "persona_voices": saved})


# ---- Pocket TTS voice cloning (Hugging Face) --------------------------------------------------

def _file_url():
    return f"{HF_ENDPOINT}/{POCKET_REPO}/resolve/{POCKET_REVISION}/{POCKET_FILE}"


async def _hf_access_problem(token):
    """None when *token* may download the cloning weights, else (status, code, message)."""
    import aiohttp as _aiohttp_client
    auth = {"Authorization": f"Bearer {token}"}
    timeout = _aiohttp_client.ClientTimeout(total=CHECK_TIMEOUT)
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get(f"{HF_ENDPOINT}/api/whoami-v2", headers=auth, timeout=timeout) as resp:
                if resp.status == 401:
                    return 400, "invalid_token", "Hugging Face didn't accept that token. Copy it again from your token page."
            async with session.head(_file_url(), headers=auth, timeout=timeout, allow_redirects=False) as resp:
                if resp.status in (401, 403):
                    return 400, "no_access", ("Hugging Face hasn't given your account the voice-cloning model yet. Open the "
                                              "model page, accept its terms, then try again. A fine-grained token also needs "
                                              "“Read access to contents of all public gated repos you can access”.")
                if resp.status not in (200, 302, 303, 307, 308):
                    return 502, "unreachable", "Hugging Face isn't answering right now. Try again in a minute."
    except Exception as exc:
        logger.warning("Hugging Face access check failed: %s", type(exc).__name__)
        return 502, "unreachable", "Hugging Face isn't reachable. Check the internet connection and try again."
    return None


class _NoTokenElsewhere(urllib.request.HTTPRedirectHandler):
    """Follow the Hub's redirect to its file storage without sending the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


def _weights_target() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "pocket-tts" / "model.safetensors"


def _download_weights(token, done_callback):
    """Fetch, verify and install the cloning weights. Runs on its own thread."""
    target = _weights_target()
    part = target.with_name(".model.safetensors.part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        opener = urllib.request.build_opener(_NoTokenElsewhere)
        req = urllib.request.Request(_file_url(), headers={"Authorization": f"Bearer {token}", "User-Agent": "JARVIS-setup"})
        digest = hashlib.sha256()
        with opener.open(req, timeout=60) as resp, open(part, "wb") as out:
            with _lock:
                _download["total"] = int(resp.headers.get("Content-Length") or 0) or POCKET_SIZE
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
                with _lock:
                    _download["done"] += len(chunk)
        if part.stat().st_size != POCKET_SIZE or digest.hexdigest() != POCKET_SHA256:
            raise RuntimeError("The download didn't match the published file, so it wasn't installed. Try again.")
        os.replace(part, target)
        with _lock:
            _download.update(state="idle", error="")
        done_callback()
    except Exception as exc:
        logger.warning("Pocket TTS cloning weights download failed: %s", type(exc).__name__)
        message = str(exc) if isinstance(exc, RuntimeError) else (
            "The voice-cloning model couldn't be downloaded. Check the internet connection and try again.")
        with _lock:
            _download.update(state="error", error=message)
    finally:
        part.unlink(missing_ok=True)


def _restart_pocket_later(loop):
    """After the weights land: stop a running Pocket worker so the next line starts one that clones."""
    from . import vesper_voice as voice

    def restart():
        if voice.POCKET_ENGINE is not None:
            asyncio.ensure_future(voice.POCKET_ENGINE.stop())
    return lambda: loop.call_soon_threadsafe(restart)


async def start_pocket_cloning(adapter, request):
    """POST /vesper/setup/voice/pocket {hf_token?} -- check the token (or the saved one), save it, and start
    downloading the cloning weights. Poll GET /vesper/setup/voice for progress."""
    from . import vesper_voice as voice
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    if not voice.pocket_installed():
        return _error(409, "not_installed", "Pocket TTS isn't part of this JARVIS image")
    if voice.pocket_cloning_weights() is not None:
        return web.json_response({"ok": True, **await asyncio.to_thread(_status_payload)})
    pasted = body.get("hf_token").strip() if isinstance(body.get("hf_token"), str) else ""
    token = pasted or await asyncio.to_thread(lambda: _saved("HF_TOKEN") or _saved("HUGGING_FACE_HUB_TOKEN"))
    if not token:
        return _error(400, "missing_token", "Paste your Hugging Face token to continue")
    if not _KEY_SHAPE.fullmatch(token):
        return _error(400, "invalid_token", "That doesn't look like a Hugging Face token (it should be one word, no spaces)")
    problem = await _hf_access_problem(token)
    if problem:
        return _error(*problem)
    if pasted:
        from hermes_cli.config import save_env_value
        await asyncio.to_thread(save_env_value, "HF_TOKEN", pasted)
    with _lock:
        starting = _download["state"] != "downloading"
        if starting:
            _download.update(state="downloading", done=0, total=0, error="")
    if starting:
        threading.Thread(target=_download_weights, args=(token, _restart_pocket_later(asyncio.get_running_loop())),
                         daemon=True, name="vesper-pocket-weights").start()
    return web.json_response({"ok": True, **await asyncio.to_thread(_status_payload)})
