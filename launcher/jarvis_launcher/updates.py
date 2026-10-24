"""Keeping a managed Hermes and its connector current.

The connector ships with this JARVIS image, so a new image brings a new connector: ``sync_connector`` copies it into
Hermes on start. Hermes itself follows its stable channel with its own updater: once a day (at the hour set on the setup
screen) ``hermes update --check``; when there is a release, stop the gateway, ``hermes update --yes``, start it and wait
until the dashboard's connector answers. If it doesn't, put back the exact commit that worked (the official installer's
``--commit``) and report what happened. External Hermes installs update themselves; JARVIS only reports the connector.
"""

import asyncio
import logging
import re
import subprocess
from datetime import datetime
from typing import Optional

from . import config, hermes_setup, state
from .managed import Gateway, InstallJob, LogBuffer, wait_healthy

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"Hermes Agent v(\S+)")


def hermes_version() -> str:
    code, out = hermes_setup.hermes("--version", timeout=60)
    match = _VERSION.search(out or "")
    return match.group(1) if match else ""


def hermes_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(config.HERMES_INSTALL_DIR), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Updater:
    def __init__(self, gateway: Gateway, installer: InstallJob):
        self.gateway = gateway
        self.installer = installer
        self.log = LogBuffer(config.STATE_DIR / "updates.log")
        self.busy = False
        self.step = ""
        self._task: Optional[asyncio.Task] = None

    # --- connector ------------------------------------------------------------------------------------------------

    async def sync_connector(self) -> bool:
        """Bring the managed Hermes's connector to this image's version; restart the gateway when it changed."""
        changed = await asyncio.to_thread(hermes_setup.install_connector, self.log.write)
        if changed and self.gateway.running:
            self.log.write("Restarting Hermes to load the updated connector")
            await self.gateway.restart()
        return changed

    # --- Hermes ---------------------------------------------------------------------------------------------------

    async def check(self) -> dict:
        code, out = await asyncio.to_thread(hermes_setup.hermes, "update", "--check", "--channel", config.HERMES_CHANNEL,
                                            timeout=300)
        text = out.strip()
        # hermes_cli/update_cmd_check.py: "Selected release available: <label>" on a release channel, "Update
        # available: N commits behind" when following a branch. Anything else (up to date, holding, newer) is no.
        available = code == 0 and ("Selected release available" in text or "Update available" in text)
        result = {"available": available, "current": hermes_version(), "output": text[-800:], "exit_code": code}
        data = state.load()
        data["updates"]["last_check"] = state.now()
        data["updates"]["last_check_result"] = result
        state.save(data)
        return result

    def start_update(self, reason: str) -> bool:
        if self.busy or self.installer.busy:
            return False
        self.busy = True
        self._task = asyncio.create_task(self._update(reason))
        return True

    async def _update(self, reason: str) -> None:
        before_version, before_commit = hermes_version(), hermes_commit()
        key = hermes_setup.read_env_value("API_SERVER_KEY")
        result = {"reason": reason, "from": before_version, "from_commit": before_commit}
        try:
            self._step(f"Updating Hermes from {before_version} ({reason})")
            await self.gateway.stop()
            code, out = await asyncio.to_thread(
                hermes_setup.hermes, "update", "--yes", "--no-gateway-restart", "--channel", config.HERMES_CHANNEL,
                timeout=1800)
            for line in out.splitlines()[-60:]:
                self.log.write(line)
            await asyncio.to_thread(hermes_setup.install_connector, self.log.write)
            self._step("Starting the updated Hermes")
            await self.gateway.start()
            problem = None if code == 0 else f"hermes update exited {code}"
            problem = problem or await wait_healthy(config.MANAGED_HERMES_URL, key)
            if problem is None:
                result.update(ok=True, to=hermes_version(), to_commit=hermes_commit())
                self.log.write(f"Hermes {result['to']} is up and the dashboard connector loaded.")
            else:
                result.update(ok=False, problem=problem)
                self.log.write(f"The update did not come up healthy ({problem}); rolling back to {before_commit[:12]}")
                await self._rollback(before_commit, key, result)
        except Exception as exc:  # noqa: BLE001 — recorded and shown on the setup screen
            logger.exception("Hermes update failed")
            result.update(ok=False, problem=str(exc))
            if not self.gateway.wanted:
                await self.gateway.start()
        finally:
            state.record_update(result)
            self.busy, self.step = False, ""

    async def _rollback(self, commit: str, key: str, result: dict) -> None:
        if not commit:
            result["rollback"] = "no previous commit recorded"
            return
        self._step(f"Rolling back to {commit[:12]}")
        await self.gateway.stop()
        script = config.STATE_DIR / "cache" / "hermes-install.sh"
        if not script.exists():
            await asyncio.to_thread(self.installer._download_installer)
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), "--non-interactive", "--skip-computer-use", "--hermes-home", str(config.HERMES_HOME),
            "--dir", str(config.HERMES_INSTALL_DIR), "--commit", commit, env=hermes_setup.hermes_env(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        for line in out.decode("utf-8", "replace").splitlines()[-40:]:
            self.log.write(line)
        await self.gateway.start()
        problem = await wait_healthy(config.MANAGED_HERMES_URL, key)
        result["rollback"] = "ok" if problem is None else f"failed: {problem}"
        self.log.write("Rolled back; Hermes is healthy again." if problem is None else f"Rollback failed: {problem}")

    def _step(self, text: str) -> None:
        self.step = text
        self.log.write(f"== {text}")

    # --- schedule -------------------------------------------------------------------------------------------------

    async def run_schedule(self) -> None:
        """Once a day at the chosen hour: check, and update when there is a release and auto-update is on."""
        while True:
            await asyncio.sleep(600)
            try:
                data = state.load()
                if data.get("mode") != "managed":
                    continue
                settings = data["updates"]
                now = datetime.now()
                last = settings.get("last_check") or ""
                if now.hour != int(settings.get("hour", config.AUTO_UPDATE_HOUR_DEFAULT)) or last[:10] == state.now()[:10]:
                    continue
                result = await self.check()
                if result["available"] and settings.get("auto", True):
                    self.start_update("scheduled")
            except Exception:  # noqa: BLE001 — the schedule must keep running
                logger.exception("update schedule tick failed")
