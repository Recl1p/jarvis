"""JARVIS's login: one password for the whole server (setup screen, dashboard, API and browser desktop).

The first visit creates the password (or JARVIS_PASSWORD sets it); after that a request needs one of:
  - the session cookie the login page sets (browsers),
  - the password as HTTP Basic auth (scripts; any user name),
  - Hermes's API key as a Bearer token (apps that talk to the Hermes API through JARVIS).
JARVIS_AUTH=off turns the login off, for a JARVIS that is only reachable through something that already
authenticates (a Tailscale tailnet, an authenticating reverse proxy).

The password is kept as a scrypt hash in the state folder; sessions as SHA-256 hashes of their tokens, so neither the
file nor a backup of it lets anyone in. Failed attempts from one address are slowed down, then locked out for a while.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional

from . import config

COOKIE = "jarvis_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 50
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
# scrypt cost: 16 MB and about 50 ms per check on a small NAS CPU.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
# Lockout: after FREE_FAILURES wrong passwords from one address, wait LOCK_SECONDS (doubling, up to LOCK_MAX).
FREE_FAILURES = 5
LOCK_SECONDS = 30
LOCK_MAX = 15 * 60

_lock = threading.Lock()
_failures: dict = {}   # remote address -> (count, locked until)


def _path():
    return config.STATE_DIR / "auth.json"


def enabled() -> bool:
    return os.environ.get("JARVIS_AUTH", "on").strip().lower() not in ("off", "0", "false", "no")


def env_password() -> str:
    return os.environ.get("JARVIS_PASSWORD", "")


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    return data


def _save(data: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(tmp, path)


def _hash(password: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                          maxmem=64 * 1024 * 1024, dklen=32)


def has_password() -> bool:
    if env_password():
        return True
    with _lock:
        return bool(_load().get("password"))


def password_problem(password) -> str:
    """Why *password* can't be used, or "" when it can."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    return ""


def set_password(password: str) -> None:
    """Store a new password and end every session (the caller logs the current browser back in)."""
    salt = secrets.token_bytes(16)
    record = {"salt": salt.hex(), "hash": _hash(password, salt, _SCRYPT).hex(), **_SCRYPT, "set_at": int(time.time())}
    with _lock:
        data = _load()
        data["password"] = record
        data["sessions"] = {}
        _save(data)
    forget_basic()


def check_password(password) -> bool:
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD:
        return False
    wanted = env_password()
    if wanted:
        return hmac.compare_digest(password.encode("utf-8"), wanted.encode("utf-8"))
    with _lock:
        record = _load().get("password")
    if not record:
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        params = {k: int(record[k]) for k in ("n", "r", "p")}
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(_hash(password, salt, params), expected)


# --- sessions ---------------------------------------------------------------------------------------------------------

def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        data = _load()
        sessions = {k: v for k, v in data["sessions"].items() if isinstance(v, (int, float)) and v > now}
        sessions[_token_id(token)] = now + SESSION_SECONDS
        if len(sessions) > MAX_SESSIONS:
            sessions = dict(sorted(sessions.items(), key=lambda kv: kv[1])[-MAX_SESSIONS:])
        data["sessions"] = sessions
        _save(data)
    return token


def valid_session(token: Optional[str]) -> bool:
    if not token or len(token) > 200:
        return False
    with _lock:
        expiry = _load()["sessions"].get(_token_id(token))
    return isinstance(expiry, (int, float)) and expiry > time.time()


def end_session(token: Optional[str]) -> None:
    if not token:
        return
    with _lock:
        data = _load()
        if data["sessions"].pop(_token_id(token), None) is not None:
            _save(data)


# --- slowing down guessing --------------------------------------------------------------------------------------------

def locked_for(remote: str) -> int:
    """Seconds until *remote* may try a password again (0 = now)."""
    count, until = _failures.get(remote or "", (0, 0.0))
    return max(0, int(until - time.time() + 0.999))


def record_failure(remote: str) -> None:
    remote = remote or ""
    count, _ = _failures.get(remote, (0, 0.0))
    count += 1
    until = 0.0
    if count >= FREE_FAILURES:
        until = time.time() + min(LOCK_MAX, LOCK_SECONDS * 2 ** (count - FREE_FAILURES))
    _failures[remote] = (count, until)
    if len(_failures) > 10_000:   # a flood of addresses: forget the oldest
        for key in list(_failures)[:5_000]:
            del _failures[key]


def record_success(remote: str) -> None:
    _failures.pop(remote or "", None)


_basic_ok: dict = {}   # SHA-256 of a Basic header that held the password -> when that stops being trusted
BASIC_CACHE_SECONDS = 300


def basic_cached(header: str) -> bool:
    expiry = _basic_ok.get(hashlib.sha256(header.encode("utf-8")).hexdigest())
    return expiry is not None and expiry > time.time()


def remember_basic(header: str) -> None:
    if len(_basic_ok) > 1000:
        _basic_ok.clear()
    _basic_ok[hashlib.sha256(header.encode("utf-8")).hexdigest()] = time.time() + BASIC_CACHE_SECONDS


def forget_basic() -> None:
    _basic_ok.clear()


def basic_password(header: str) -> Optional[str]:
    """The password in an ``Authorization: Basic`` header, or None."""
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded.split(":", 1)[1] if ":" in decoded else None
