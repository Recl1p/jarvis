"""The dashboard user's preferences, kept on the server so every device and the home-screen app share them:
the persona (which is also the theme and the app icon), how to be addressed, the layout, clock and temperature
units, the audio source, the voice engine, the last model picked in the model picker and the chat in use.

GET /vesper/prefs returns them; PUT /vesper/prefs merges the keys it is given (null removes one). The dashboard page
also carries them on <body data-vesper-prefs>, so a fresh device or a home-screen launch starts with the right
persona instead of flashing the default first. Single user, like the rest of the dashboard: one file in HERMES_HOME.
"""

import asyncio
import json
import re
import threading
from functools import partial
from pathlib import Path

from aiohttp import web

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def _choice(*options):
    return lambda v: v if v in options else None


def _text(limit):
    return lambda v: " ".join(v.split())[:limit] if isinstance(v, str) else None


def _flag(v):
    return v if isinstance(v, bool) else None


def _ident(v):
    return v if isinstance(v, str) and _ID.match(v) else None


def _model(v):
    if not isinstance(v, dict):
        return None
    provider, model = _ident(v.get("provider")), _ident(v.get("id"))
    return {"provider": provider, "id": model} if provider and model else None


def _persona(v):
    return v if isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", v) else None


# name -> validator (returns the clean value, or None when the value is not acceptable)
PREFS = {
    "persona": _persona,
    "honorific": _text(40),
    "layout": _choice("glass", "cinematic"),
    "clock12": _flag,
    "tempUnit": _choice("C", "F"),
    "audioSource": _choice("radio", "library"),
    "voiceEngine": _choice("elevenlabs", "pocket", "omnivoice", "edge", "piper"),
    "model": _model,
    "session": _ident,
}


def http_routes(adapter):
    return [
        ("GET", "/vesper/prefs", partial(get_prefs, adapter)),
        ("PUT", "/vesper/prefs", partial(put_prefs, adapter)),
    ]


def prefs_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "vesper-prefs.json"


def read_prefs() -> dict:
    """The saved preferences, dropping anything a hand edit or an older version left that no longer validates."""
    try:
        raw = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for name, check in PREFS.items():
        if name in raw and raw[name] is not None:
            value = check(raw[name])
            if value is not None:
                clean[name] = value
    return clean


def merge_prefs(current: dict, changes: dict) -> dict:
    """*current* with *changes* applied. Raises ValueError naming the first unknown key or invalid value."""
    if not isinstance(changes, dict):
        raise ValueError("Expected a JSON object of preferences")
    merged = dict(current)
    for name, value in changes.items():
        if name not in PREFS:
            raise ValueError(f"Unknown preference: {name}")
        if value is None:
            merged.pop(name, None)
            continue
        clean = PREFS[name](value)
        if clean is None:
            raise ValueError(f"Invalid value for {name}")
        merged[name] = clean
    return merged


_lock = threading.Lock()   # two devices saving at once must not drop each other's keys


def save_changes(changes: dict) -> dict:
    from .vesper import _write_note_file

    with _lock:
        merged = merge_prefs(read_prefs(), changes)
        _write_note_file(prefs_path(), merged)
    return merged


async def get_prefs(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response({"prefs": await asyncio.to_thread(read_prefs)})


async def put_prefs(adapter, request):
    from gateway.platforms import api_server

    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        changes = await request.json()
    except (ValueError, LookupError):
        return api_server._error_response("Invalid JSON body", 400, code="invalid_json")
    try:
        merged = await asyncio.to_thread(save_changes, changes)
    except ValueError as exc:
        return api_server._error_response(str(exc), 400, code="invalid_prefs")
    return web.json_response({"prefs": merged})
