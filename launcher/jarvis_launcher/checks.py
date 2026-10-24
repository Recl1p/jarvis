"""The "Test connection" checks: can JARVIS reach this Hermes, use it, and show its dashboard?

Each check says what it looked at, whether it passed, and (when not) the one thing to do about it. Later checks are
skipped once an earlier one makes them meaningless (no point testing the API key of a server that isn't Hermes).
"""

import asyncio
import json
from typing import Optional

import aiohttp

from . import config, hermes_setup


def _check(cid: str, label: str, ok: Optional[bool], detail: str = "", hint: str = "") -> dict:
    return {"id": cid, "label": label, "ok": ok, "detail": detail, "hint": hint}


def _version_tuple(text: str) -> tuple:
    parts = []
    for piece in str(text or "").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


async def run(url: str, key: str, *, timeout: float = 8) -> dict:
    url = url.rstrip("/")
    checks = []
    info = {"hermes_version": "", "connector_version": "", "bundled_connector_version": hermes_setup.bundled_connector_version()}
    auth = {"Authorization": f"Bearer {key}"} if key else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async def get(path, headers=None):
            async with session.get(url + path, headers=headers or {}, allow_redirects=False) as resp:
                body = await resp.read()
                return resp.status, resp.headers.get("Content-Type", ""), body

        # 1. Something answers at this address.
        try:
            status, _, body = await get("/health")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            checks.append(_check("reach", "Hermes answers at this address", False, f"{type(exc).__name__}: {exc}"[:200],
                                 "Start Hermes's gateway (hermes gateway run) and enable its API server. If Hermes runs on "
                                 "another computer, its API server must listen on 0.0.0.0, not 127.0.0.1, and port "
                                 f"{url.rsplit(':', 1)[-1].split('/')[0]} must be open."))
            return {"ok": False, "checks": checks, **info}
        checks.append(_check("reach", "Hermes answers at this address", status == 200, f"HTTP {status}"))
        if status != 200:
            return {"ok": False, "checks": checks, **info}

        # 2. It is Hermes Agent.
        try:
            health = json.loads(body or b"{}")
        except ValueError:
            health = {}
        is_hermes = health.get("platform") == "hermes-agent"
        info["hermes_version"] = str(health.get("version") or "")
        checks.append(_check("hermes", "It is Hermes Agent", is_hermes,
                             f"version {info['hermes_version']}" if is_hermes else "this server is not a Hermes API server",
                             "" if is_hermes else "Check the address: it should be the Hermes API server (usually port 8642)."))
        if not is_hermes:
            return {"ok": False, "checks": checks, **info}
        old = _version_tuple(info["hermes_version"]) < _version_tuple(config.HERMES_MIN_VERSION)
        if old and info["hermes_version"] not in ("", "0.0.0"):
            checks.append(_check("version", f"Hermes {config.HERMES_MIN_VERSION} or newer", False, info["hermes_version"],
                                 "Update Hermes with `hermes update`, then test again."))
            return {"ok": False, "checks": checks, **info}

        # 3. The API key works.
        status, _, _ = await get("/v1/models", auth)
        key_ok = status == 200
        checks.append(_check("auth", "The API key is accepted", key_ok,
                             "accepted" if key_ok else (f"HTTP {status}" if key else "no key given"),
                             "" if key_ok else "Use the API_SERVER_KEY from that Hermes's .env file (~/.hermes/.env)."))
        if not key_ok:
            return {"ok": False, "checks": checks, **info}

        # 4. The JARVIS connector plugin is installed and enabled there.
        status, _, body = await get("/vesper/connector")
        has_connector = status == 200
        if has_connector:
            try:
                info["connector_version"] = str(json.loads(body).get("version") or "")
            except ValueError:
                pass
        checks.append(_check("connector", "The JARVIS connector is installed in Hermes", has_connector,
                             f"version {info['connector_version']}" if has_connector else "not found",
                             "" if has_connector else "Paste the connection prompt into your Hermes (or run the connect "
                             "script) to install it, then test again."))
        if not has_connector:
            return {"ok": False, "checks": checks, **info}

        # 5. The connector is this JARVIS's version (an older one still works, but may miss dashboard features).
        current = info["connector_version"] == info["bundled_connector_version"]
        checks.append(_check("connector_version", "The connector is up to date", True if current else None,
                             f"{info['connector_version']} (this JARVIS ships {info['bundled_connector_version']})",
                             "" if current else "Run the connection prompt or script again to update it."))

        # 6. Plugin isolation lets the connector serve the dashboard (authenticated check).
        status, _, body = await get("/vesper/connector/check", auth)
        isolation = ""
        if status == 200:
            try:
                isolation = str(json.loads(body).get("isolation") or "")
            except ValueError:
                pass
        in_process = status == 200 and isolation in ("in_process", "")
        checks.append(_check("isolation", "Plugins run inside Hermes (in_process)", in_process,
                             isolation or f"HTTP {status}",
                             "" if in_process else "Set plugins.isolation: in_process in config.yaml "
                             "(hermes config set plugins.isolation in_process) and restart the gateway."))

        # 7. The dashboard itself loads (the connector serves it only to requests JARVIS vouches for).
        status, ctype, _ = await get("/vesper", {"X-JARVIS-Launcher": key} if key else None)
        dash_ok = status == 200 and "text/html" in ctype
        checks.append(_check("dashboard", "The JARVIS dashboard loads", dash_ok, f"HTTP {status}",
                             "" if dash_ok else "Restart the Hermes gateway so it loads the connector."))

        # 8. Which model Hermes is set to (informational: having a model's credentials is the setup guide's job).
        status, _, body = await get("/vesper/setup/status", auth)
        model = ""
        if status == 200:
            try:
                data = json.loads(body)
                model = " / ".join(str(v) for v in (data.get("provider"), data.get("model")) if v)
            except ValueError:
                pass
        checks.append(_check("model", "Model set in Hermes", None, model or "none yet",
                             "The setup guide in the dashboard connects one (and signs in to its provider)."))
    ok = all(c["ok"] is not False for c in checks)
    return {"ok": ok, "checks": checks, **info}
