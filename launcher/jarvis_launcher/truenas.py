"""Hermes as a TrueNAS app: read, upgrade, roll back and restart that app through the TrueNAS API.

The API is JSON-RPC 2.0 over a WebSocket (``wss://<nas>/api/current``). JARVIS logs in with the API key of a TrueNAS
user that has only the APPS_WRITE role (config.TRUENAS_API_KEY, from the container's environment). TrueNAS revokes an
API key that is ever sent over plain HTTP, so only ``wss://`` is accepted. A NAS's certificate is usually self-signed;
config.TRUENAS_CERT_SHA256 pins it instead.

Long operations (upgrade, rollback, redeploy) are TrueNAS jobs: the call returns a job id, and the job is polled until
it ends, passing its progress messages on.
"""

import asyncio
import itertools
import json
import logging
from typing import Callable, Optional

import aiohttp

from . import config

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]

JOB_POLL_SECONDS = 2
JOB_TIMEOUT_SECONDS = 1800
CALL_TIMEOUT_SECONDS = 60


class TrueNASError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.TRUENAS_URL and config.TRUENAS_API_KEY and config.TRUENAS_APP)


def _secure(url: str) -> bool:
    return url.startswith("wss://")


def _ssl():
    pin = config.TRUENAS_CERT_SHA256.replace(":", "").strip().lower()
    if not pin:
        return True        # ordinary certificate verification
    try:
        digest = bytes.fromhex(pin)
    except ValueError as exc:
        raise TrueNASError("JARVIS_TRUENAS_CERT_SHA256 is not a hex SHA-256 fingerprint") from exc
    if len(digest) != 32:
        raise TrueNASError("JARVIS_TRUENAS_CERT_SHA256 must be a SHA-256 fingerprint (64 hex digits)")
    return aiohttp.Fingerprint(digest)


def _error_text(error) -> str:
    if not isinstance(error, dict):
        return str(error)
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    return str(data.get("reason") or error.get("message") or error)[:500]


class Client:
    """One logged-in API connection: ``async with Client() as api: await api.call("app.query", ...)``."""

    def __init__(self, url: str = "", key: str = ""):
        self.url = url or config.TRUENAS_URL
        self.key = key or config.TRUENAS_API_KEY
        self._ids = itertools.count(1)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None

    async def __aenter__(self) -> "Client":
        if not _secure(self.url):
            raise TrueNASError("The TrueNAS API address must start with wss:// (TrueNAS revokes a key sent over http).")
        if not self.key:
            raise TrueNASError("No TrueNAS API key is set (JARVIS_TRUENAS_API_KEY).")
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        try:
            options = {"ssl": _ssl()} if self.url.startswith("wss://") else {}
            self._ws = await self._session.ws_connect(self.url, heartbeat=30, max_msg_size=64 * 1024 * 1024, **options)
            if await self.call("auth.login_with_api_key", self.key) is not True:
                raise TrueNASError("TrueNAS did not accept JARVIS's API key.")
        except aiohttp.ServerFingerprintMismatch as exc:
            await self._session.close()
            raise TrueNASError("The NAS's certificate is not the one JARVIS_TRUENAS_CERT_SHA256 pins (TrueNAS renews "
                               "its self-signed certificate when it expires). Put the new certificate's SHA-256 in "
                               "JARVIS_TRUENAS_CERT_SHA256.") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            await self._session.close()
            raise TrueNASError(f"Can't reach the TrueNAS API at {self.url}: {type(exc).__name__}: {exc}"[:400]) from exc
        except BaseException:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def call(self, method: str, *params, timeout: float = CALL_TIMEOUT_SECONDS):
        call_id = next(self._ids)
        await self._ws.send_str(json.dumps({"jsonrpc": "2.0", "id": call_id, "method": method, "params": list(params)}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TrueNASError(f"TrueNAS did not answer {method} within {int(timeout)} s.")
            try:
                message = await asyncio.wait_for(self._ws.receive(), remaining)
            except asyncio.TimeoutError:
                continue
            if message.type != aiohttp.WSMsgType.TEXT:
                raise TrueNASError(f"The TrueNAS API connection closed during {method}.")
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            if data.get("id") != call_id:
                continue       # event notifications and other replies
            if "error" in data:
                raise TrueNASError(f"{method}: {_error_text(data['error'])}")
            return data.get("result")

    async def job(self, method: str, *params, progress: Optional[Progress] = None,
                  timeout: float = JOB_TIMEOUT_SECONDS):
        job_id = await self.call(method, *params)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        said = ""
        while True:
            jobs = await self.call("core.get_jobs", [["id", "=", job_id]])
            job = jobs[0] if jobs else {}
            step = ((job.get("progress") or {}).get("description") or "").strip()
            if progress and step and step != said:
                said = step
                progress(step)
            state = job.get("state")
            if state == "SUCCESS":
                return job.get("result")
            if state in ("FAILED", "ABORTED"):
                raise TrueNASError(f"{method} {state.lower()}: {str(job.get('error') or '')[:500]}")
            if loop.time() > deadline:
                raise TrueNASError(f"{method} was still running after {int(timeout)} s.")
            await asyncio.sleep(JOB_POLL_SECONDS)


# --- the Hermes app --------------------------------------------------------------------------------------------------

def _summary(app: dict) -> dict:
    meta = app.get("metadata") if isinstance(app.get("metadata"), dict) else {}
    return {"name": app.get("name") or config.TRUENAS_APP, "state": app.get("state") or "",
            "version": str(app.get("version") or ""), "human_version": str(app.get("human_version") or ""),
            "app_version": str(meta.get("app_version") or ""),
            "upgrade_available": bool(app.get("upgrade_available")),
            "latest_version": str(app.get("latest_version") or ""),
            "latest_app_version": str(app.get("latest_app_version") or "")}


async def app_info() -> dict:
    async with Client() as api:
        apps = await api.call("app.query", [["name", "=", config.TRUENAS_APP]])
    if not apps:
        raise TrueNASError(f"TrueNAS has no app named {config.TRUENAS_APP!r}.")
    return _summary(apps[0])


async def upgrade(progress: Optional[Progress] = None) -> dict:
    """Upgrade the app to the catalog's latest version. TrueNAS snapshots its host-path volumes first."""
    async with Client() as api:
        await api.job("app.upgrade", config.TRUENAS_APP, {"app_version": "latest", "snapshot_hostpaths": True},
                      progress=progress)
        apps = await api.call("app.query", [["name", "=", config.TRUENAS_APP]])
    return _summary(apps[0]) if apps else {}


async def rollback(version: str, progress: Optional[Progress] = None) -> dict:
    async with Client() as api:
        await api.job("app.rollback", config.TRUENAS_APP, {"app_version": version, "rollback_snapshot": True},
                      progress=progress)
        apps = await api.call("app.query", [["name", "=", config.TRUENAS_APP]])
    return _summary(apps[0]) if apps else {}


async def redeploy(progress: Optional[Progress] = None) -> None:
    """Recreate the app's container (Hermes restarts and loads a changed connector or voice runtime)."""
    async with Client() as api:
        await api.job("app.redeploy", config.TRUENAS_APP, progress=progress, timeout=600)
