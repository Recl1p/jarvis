"""A Hermes that JARVIS installs and runs itself ("managed" mode).

``InstallJob`` downloads the official Hermes installer only after the user accepted it on the welcome screen, runs it
into the data volume, sets Hermes up for JARVIS and starts it. ``Gateway`` starts and stops that Hermes's gateway, which
Hermes itself supervises as an s6 service in this container.
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
import urllib.request
from collections import deque
from typing import Optional

import aiohttp

from . import config, hermes_setup, state

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 3 * 1024 ** 3
# Installer progress bars ("node:  49.2%  16.0 MiB / 32.5 MiB") are one line per tick; keep only the finished ones.
_PROGRESS = re.compile(r"^\s*\S+:\s+\d{1,3}(\.\d)?%\s")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class LogBuffer:
    """Recent lines for the setup screen, mirrored to a file for later reading."""

    def __init__(self, path, keep: int = 2000):
        self.lines = deque(maxlen=keep)
        self.offset = 0          # how many lines were ever written (the client polls from its last offset)
        self.path = path

    def write(self, line: str) -> None:
        line = _ANSI.sub("", line.rstrip("\r\n"))
        if _PROGRESS.match(line) and "100.0%" not in line:
            return
        self.lines.append(line)
        self.offset += 1
        try:
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    def since(self, offset: int) -> tuple:
        start = max(offset, self.offset - len(self.lines))
        skip = start - (self.offset - len(self.lines))
        return list(self.lines)[skip:], self.offset


async def wait_healthy(url: str, key: str, timeout: int = config.HEALTH_TIMEOUT_SECONDS) -> Optional[str]:
    """Wait until Hermes answers /health and its connector answers an authenticated check. None = healthy, else why not."""
    deadline = time.monotonic() + timeout
    problem = "Hermes did not answer"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{url}/health") as resp:
                    if resp.status == 200:
                        async with session.get(f"{url}/vesper/connector/check",
                                               headers={"Authorization": f"Bearer {key}"}) as check:
                            if check.status == 200:
                                return None
                            problem = f"the JARVIS connector did not load (HTTP {check.status})"
                    else:
                        problem = f"Hermes answered HTTP {resp.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                problem = "Hermes is not answering yet"
            await asyncio.sleep(3)
    return problem


def gateway_log_tail(lines: int = 15) -> list:
    """The last lines Hermes's gateway wrote (s6-log, logs/gateways/default/current), for an error message."""
    path = config.HERMES_HOME / "logs" / "gateways" / "default" / "current"
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return [f"(no gateway log at {path})"]


class Gateway:
    """The managed Hermes gateway, supervised by Hermes itself.

    Inside this s6 container ``hermes gateway start`` registers the gateway as an s6 service (auto-restart on crash,
    restored on container restart by Hermes's boot reconciler, see image/cont-init.d/20-hermes-s6), exactly as in
    Hermes's own image. So JARVIS only asks: start, stop, restart, and "does it answer?".
    """

    def __init__(self):
        self.wanted = False
        self.restarts = 0
        self._lock = asyncio.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def _cli(self, action: str, timeout: int = 180) -> tuple:
        code, out = await asyncio.to_thread(hermes_setup.hermes, "gateway", action, timeout=timeout)
        if code != 0:
            logger.warning("hermes gateway %s exited %s: %s", action, code, out.strip()[-400:])
        return code, out

    async def start(self) -> tuple:
        async with self._lock:
            self.wanted = True
            code, out = await self._cli("start")
            for _ in range(3):
                if code == 0:
                    break
                # On a first start Hermes registers its s6 slot and starts it in the same breath, but s6 picks a new
                # slot up asynchronously ("supervisor not listening"). Wait until s6 supervises it, then start again.
                await self._wait_supervised()
                code, out = await self._cli("start")
            self._running = code == 0
            return code, out

    @staticmethod
    async def _wait_supervised(seconds: float = 15) -> bool:
        slot = "/run/service/gateway-default"
        svok = "/command/s6-svok" if os.path.exists("/command/s6-svok") else "s6-svok"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(svok, slot, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)
                if await proc.wait() == 0:
                    return True
            except OSError:
                return False
            await asyncio.sleep(0.5)
        return False

    async def stop(self, grace: int = 90) -> None:
        async with self._lock:
            self.wanted = False
            await self._cli("stop", timeout=grace + 30)
            self._running = False

    async def restart(self) -> tuple:
        async with self._lock:
            self.wanted = True
            self.restarts += 1
            result = await self._cli("restart", timeout=240)
            self._running = True
            return result


class InstallJob:
    """Install Hermes with the official installer, set it up for JARVIS, start it. One at a time."""

    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.log = LogBuffer(config.STATE_DIR / "install.log")
        self.status = "idle"      # idle | running | done | failed
        self.step = ""
        self.error = ""
        self._task: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, *, persona: bool) -> bool:
        if self.busy:
            return False
        self.status, self.error, self.step = "running", "", ""
        self._task = asyncio.create_task(self._run(persona))
        return True

    def _step(self, name: str) -> None:
        self.step = name
        self.log.write(f"== {name}")

    async def _run(self, persona: bool) -> None:
        try:
            self._step("Checking free space")
            free = shutil.disk_usage(config.HERMES_HOME).free
            if free < MIN_FREE_BYTES:
                raise RuntimeError(f"Only {free // 1024 ** 2} MB free in {config.HERMES_HOME}; Hermes needs about 3 GB.")
            self.log.write(f"{free // 1024 ** 3} GB free in {config.HERMES_HOME}")

            if hermes_setup.installed():
                # A retry after a failed start, or a volume that already has Hermes: no need to reinstall.
                self.log.write(f"Hermes is already installed in {config.HERMES_INSTALL_DIR}; keeping it.")
            else:
                if (config.HERMES_HOME / "config.yaml").exists():
                    self._step("Preparing your existing JARVIS data")
                    await asyncio.to_thread(hermes_setup.prepare_old_volume, self.log.write)
                self._step("Downloading the official Hermes installer")
                script, digest = await asyncio.to_thread(self._download_installer)
                self.log.write(f"{config.HERMES_INSTALLER_URL} (sha256 {digest})")
                consent = {**(state.load().get("consent") or {}), "installer_sha256": digest}
                state.update(consent=consent)

                self._step("Installing Hermes Agent (Nous Research's installer)")
                args = ["--non-interactive", "--skip-computer-use", "--hermes-home", str(config.HERMES_HOME),
                        "--dir", str(config.HERMES_INSTALL_DIR), "--channel", config.HERMES_CHANNEL]
                if config.HERMES_COMMIT:
                    args += ["--commit", config.HERMES_COMMIT]
                code = await self._stream(["bash", str(script), *args])
                if code != 0 or not hermes_setup.installed():
                    raise RuntimeError(f"The Hermes installer stopped with exit code {code}; see the log above.")
            self._step("Setting up Hermes for JARVIS")
            await asyncio.to_thread(hermes_setup.apply, self.log.write, managed=True, persona=persona)

            self._step("Starting Hermes")
            code, out = await self.gateway.start()
            self._log_cli("hermes gateway start", code, out)
            key = hermes_setup.read_env_value("API_SERVER_KEY")
            problem = await wait_healthy(config.MANAGED_HERMES_URL, key, timeout=150)
            if problem:
                # A first start can stall (a slow machine, a gateway that lost a race with its own registration):
                # one restart before giving up.
                self.log.write(f"Hermes isn't answering yet ({problem}); restarting it once")
                code, out = await self.gateway.restart()
                self._log_cli("hermes gateway restart", code, out)
                problem = await wait_healthy(config.MANAGED_HERMES_URL, key)
            if problem:
                for line in gateway_log_tail():
                    self.log.write(f"   {line}")
                raise RuntimeError(f"Hermes is installed but not ready: {problem}. The end of its log is above; "
                                   f"all of it is in {config.HERMES_HOME / 'logs' / 'gateways'}. Try again restarts it.")
            state.update(mode="managed", hermes_url=config.MANAGED_HERMES_URL, api_key="", persona=persona)
            self.step = "Done"
            self.log.write("== Hermes is installed and connected. Opening JARVIS…")
            self.status = "done"
        except Exception as exc:  # noqa: BLE001 — every failure is shown on the setup screen
            logger.exception("Hermes install failed")
            self.error = str(exc)
            self.log.write(f"!! {exc}")
            self.status = "failed"

    def _log_cli(self, command: str, code: int, out: str) -> None:
        lines = [line for line in out.strip().splitlines() if line.strip()][-6:]
        self.log.write(f"{command}: exit {code}" + (":" if lines else ""))
        for line in lines:
            self.log.write(f"   {line}")

    def _download_installer(self) -> tuple:
        cache = config.STATE_DIR / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / "hermes-install.sh"
        request = urllib.request.Request(config.HERMES_INSTALLER_URL, headers={"User-Agent": "JARVIS-launcher"})
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read()
        if not body.startswith(b"#!"):
            raise RuntimeError("The download is not a shell script; the Hermes site may be down. Try again later.")
        target.write_bytes(body)
        return target, hashlib.sha256(body).hexdigest()

    async def _stream(self, argv: list) -> int:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=hermes_setup.hermes_env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, cwd=str(config.HERMES_HOME))
        assert proc.stdout is not None
        pending = b""
        while chunk := await proc.stdout.read(4096):
            pending += chunk
            # Progress bars redraw with \r; treat it as a line break so each tick is its own line.
            *lines, pending = re.split(rb"\r\n|\n|\r", pending)
            for line in lines:
                self.log.write(line.decode("utf-8", "replace"))
        if pending:
            self.log.write(pending.decode("utf-8", "replace"))
        return await proc.wait()
