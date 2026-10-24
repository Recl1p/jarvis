"""The launcher's saved state: which Hermes JARVIS uses, the install consent, auto-update settings and history.

One JSON file, owner-only (it can hold an external Hermes's API key), written atomically.
"""

import json
import os
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()


def _path():
    return config.STATE_DIR / "state.json"


DEFAULTS = {
    "mode": None,            # None (not set up) | "managed" (JARVIS installed Hermes) | "external" (the user's own)
    "hermes_url": "",        # external mode: where that Hermes's API server is
    "api_key": "",           # external mode: its API_SERVER_KEY (managed mode reads it from HERMES_HOME/.env)
    "consent": None,         # managed mode: {accepted_at, installer_url, installer_sha256, channel}
    "persona": True,         # apply the JARVIS persona (SOUL.md) during setup
    "updates": {"auto": config.AUTO_UPDATE_DEFAULT, "hour": config.AUTO_UPDATE_HOUR_DEFAULT,
                "last_check": None, "last_result": None, "history": []},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    with _lock:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    merged = {**DEFAULTS, **(data if isinstance(data, dict) else {})}
    merged["updates"] = {**DEFAULTS["updates"], **(merged.get("updates") or {})}
    return merged


def save(data: dict) -> None:
    with _lock:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _path()
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(tmp, path)


def update(**changes) -> dict:
    data = load()
    data.update(changes)
    save(data)
    return data


def record_update(result: dict) -> None:
    """Append one update attempt to the history (last 20 kept) and make it the last result."""
    data = load()
    updates = data["updates"]
    entry = {"at": now(), **result}
    updates["last_result"] = entry
    updates["history"] = (updates.get("history") or [])[-19:] + [entry]
    save(data)
