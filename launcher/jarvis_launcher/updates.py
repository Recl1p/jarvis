"""Keeping Hermes and its connector current.

Two kinds of Hermes can be updated from the setup screen (``kind()``):

``managed``  the Hermes JARVIS installed into its data volume. It follows its stable channel with its own updater:
             ``hermes update --check``; when there is a release, stop the gateway, ``hermes update --yes``, start it and
             wait until the dashboard's connector answers. If it doesn't, put back the exact commit that worked (the
             official installer's ``--commit``) and report what happened.
``truenas``  an external Hermes that runs as a TrueNAS app (truenas.py): the app is upgraded to the catalog's latest
             version through the TrueNAS API (which snapshots the app's host-path volumes first), and rolled back to the
             version before if the upgraded Hermes doesn't come up with the connector loaded.

Either way it happens once a day at the hour set on the setup screen, or when "Update now" is pressed. A release that
already failed is not retried by the schedule (it would take Hermes down every night); pressing the button still does.
Other external Hermes installs update themselves; JARVIS only reports the connector.

The connector ships with this JARVIS image, so a new image brings a new connector: ``sync_connector`` copies it into a
Hermes whose home JARVIS can write (managed, or an external one sharing this container's data folder) and restarts it.
"""

import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime
from typing import Optional

import aiohttp

from . import config, hermes_setup, state, truenas
from .managed import Gateway, InstallJob, LogBuffer, wait_healthy

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"Hermes Agent v(\S+)")
_RELEASE = re.compile(r"Selected release available:\s*(.+?)\s*$", re.M)


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


def kind(data: Optional[dict] = None) -> Optional[str]:
    """Which Hermes the Updates card can update: "managed", "truenas", or None (an external Hermes updates itself)."""
    mode = (data or state.load()).get("mode")
    if mode == "managed":
        return "managed"
    if mode == "external" and truenas.configured():
        return "truenas"
    return None


async def health(url: str) -> dict:
    """Hermes's /health (it names the version), or {} when it doesn't answer."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(f"{url.rstrip('/')}/health") as resp:
                if resp.status == 200:
                    return json.loads(await resp.read() or b"{}")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        pass
    return {}


async def running_connector_version(url: str) -> str:
    """The connector version the running Hermes loaded (its public /vesper/connector route), "" if none answers."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(f"{url.rstrip('/')}/vesper/connector") as resp:
                if resp.status == 200:
                    data = json.loads(await resp.read() or b"{}")
                    return str(data.get("connector_version") or data.get("version") or "")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        pass
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

    async def sync_shared(self, url: str) -> None:
        """An external Hermes in another container that shares this data folder: copy this image's connector into it,
        and restart that Hermes (through TrueNAS, when it is a TrueNAS app) if it runs another connector version."""
        changed = await asyncio.to_thread(hermes_setup.install_connector, self.log.write)
        running = await running_connector_version(url)
        bundled = hermes_setup.bundled_connector_version()
        # image/cont-init.d/25-jarvis-runtime copied a new voice runtime: Hermes still has the old folders mounted.
        runtime_marker = config.RUNTIME_DIR / ".changed"
        runtime_changed = runtime_marker.exists()
        if not changed and not runtime_changed and (not running or running == bundled):
            await asyncio.to_thread(self._drop_old_runtime)
            return
        what = " and ".join(part for part, needed in (
            (f"connector {bundled}", changed or (running and running != bundled)),
            ("the new voice runtime", runtime_changed)) if needed)
        if not truenas.configured():
            self.log.write(f"Hermes runs connector {running or '(none)'}; restart it to load {what}.")
            return
        self._step(f"Restarting the Hermes app to load {what}")
        try:
            await truenas.redeploy(progress=lambda s: self.log.write(f"   {s}"))
        except truenas.TrueNASError as exc:
            self.log.write(f"Could not restart the Hermes app: {exc}")
            self.step = ""
            return
        problem = await wait_healthy(url, state.load().get("api_key") or "")
        self.step = ""
        if problem is None:
            self.log.write(f"Hermes is back with {what}.")
            runtime_marker.unlink(missing_ok=True)
            await asyncio.to_thread(self._drop_old_runtime)
        else:
            self.log.write(f"Hermes after the restart: {problem}")

    @staticmethod
    def _drop_old_runtime() -> None:
        """The voice runtime folders a restarted Hermes no longer has mounted (25-jarvis-runtime keeps them until then)."""
        import shutil
        for old in config.RUNTIME_DIR.glob("*.old"):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old, ignore_errors=True)

    # --- checking ---------------------------------------------------------------------------------------------------

    async def check(self) -> dict:
        data = state.load()
        if kind(data) == "truenas":
            result = await self._check_app(data)
        else:
            code, out = await asyncio.to_thread(hermes_setup.hermes, "update", "--check", "--channel",
                                                config.HERMES_CHANNEL, timeout=300)
            text = out.strip()
            # hermes_cli/update_cmd_check.py: "Selected release available: <label>" on a release channel, "Update
            # available: N commits behind" when following a branch. Anything else (up to date, holding, newer) is no.
            available = code == 0 and ("Selected release available" in text or "Update available" in text)
            release = _RELEASE.search(text)
            result = {"available": available, "current": hermes_version(), "output": text[-800:], "exit_code": code,
                      "target": release.group(1) if release else ("branch head" if available else "")}
        data = state.load()
        data["updates"]["last_check"] = state.now()
        data["updates"]["last_check_result"] = result
        state.save(data)
        return result

    async def _check_app(self, data: dict) -> dict:
        current = str((await health(data.get("hermes_url") or "")).get("version") or "")
        try:
            app = await truenas.app_info()
        except truenas.TrueNASError as exc:
            return {"available": False, "current": current, "output": str(exc), "exit_code": 1, "target": ""}
        if app["upgrade_available"]:
            text = (f"TrueNAS app {app['name']} {app['version']} (Hermes {app['app_version'] or current}) can be "
                    f"upgraded to {app['latest_version']}"
                    + (f" (Hermes {app['latest_app_version']})" if app["latest_app_version"] else "") + ".")
        else:
            text = f"TrueNAS app {app['name']} {app['version']} (Hermes {app['app_version'] or current}) is the latest."
        return {"available": app["upgrade_available"], "current": current, "output": text, "exit_code": 0,
                "target": app["latest_version"] if app["upgrade_available"] else "", "app": app}

    # --- updating ---------------------------------------------------------------------------------------------------

    def start_update(self, reason: str) -> bool:
        if self.busy or self.installer.busy:
            return False
        self.busy = True
        run = self._update_app if kind() == "truenas" else self._update
        self._task = asyncio.create_task(run(reason))
        return True

    async def _update(self, reason: str) -> None:
        before_version, before_commit = hermes_version(), hermes_commit()
        key = hermes_setup.read_env_value("API_SERVER_KEY")
        target = (state.load()["updates"].get("last_check_result") or {}).get("target") or ""
        result = {"reason": reason, "from": before_version, "from_commit": before_commit, "target": target}
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
            started, said = await self.gateway.start()
            if started != 0:
                # The reason the gateway can't start, which the health check below can only call "not answering".
                self.log.write(f"hermes gateway start exited {started}: {said.strip()[-400:]}")
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

    async def _update_app(self, reason: str) -> None:
        data = state.load()
        url, key = data.get("hermes_url") or "", data.get("api_key") or ""
        progress = lambda step: self.log.write(f"   {step}")  # noqa: E731
        result = {"reason": reason, "via": "truenas", "from": str((await health(url)).get("version") or "")}
        try:
            app = await truenas.app_info()
            result.update(from_app=app["version"], target=app["latest_version"])
            self._step(f"Updating the Hermes app {app['name']} from {app['version']} (Hermes {result['from']}) "
                       f"({reason})")
            if not app["upgrade_available"]:
                self.log.write("TrueNAS has no newer version of the app.")
                result.update(ok=True, to=result["from"], to_app=app["version"])
                return
            self.log.write(f"Upgrading to {app['latest_version']} through TrueNAS (it snapshots the app's folders first)")
            problem = None
            try:
                await truenas.upgrade(progress=progress)
            except truenas.TrueNASError as exc:
                problem = f"TrueNAS could not upgrade the app: {exc}"
                self.log.write(problem)
            self._step("Waiting for the updated Hermes")
            problem = await wait_healthy(url, key) if problem is None else problem
            after = await truenas.app_info()
            if problem is None:
                result.update(ok=True, to=str((await health(url)).get("version") or ""), to_app=after["version"])
                self.log.write(f"Hermes {result['to']} (app {after['version']}) is up and the dashboard connector "
                               f"loaded.")
                return
            result.update(ok=False, problem=problem)
            if after["version"] == app["version"]:
                # The upgrade never took: the app still runs the version it had. Nothing to roll back.
                result["rollback"] = "not needed (the app kept its version)"
                return
            self.log.write(f"The update did not come up healthy ({problem}); rolling the app back to {app['version']}")
            self._step(f"Rolling the Hermes app back to {app['version']}")
            await truenas.rollback(app["version"], progress=progress)
            problem = await wait_healthy(url, key)
            result["rollback"] = "ok" if problem is None else f"failed: {problem}"
            self.log.write("Rolled back; Hermes is healthy again." if problem is None else f"Rollback failed: {problem}")
        except Exception as exc:  # noqa: BLE001 — recorded and shown on the setup screen
            logger.exception("Hermes app update failed")
            result.update(ok=False, problem=str(exc))
            self.log.write(f"!! {exc}")
        finally:
            state.record_update(result)
            self.busy, self.step = False, ""

    def _step(self, text: str) -> None:
        self.step = text
        self.log.write(f"== {text}")

    # --- schedule -------------------------------------------------------------------------------------------------

    @staticmethod
    def _checked_today(last: str, now: datetime) -> bool:
        """The saved check time is UTC; the hour on the setup screen is the container's local time (TZ)."""
        try:
            return datetime.fromisoformat(last).astimezone(now.astimezone().tzinfo).date() == now.date()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _failed_before(settings: dict, target: str) -> Optional[dict]:
        """The last update attempt, when it went to this same release and failed."""
        last = settings.get("last_result") or {}
        if target and last.get("ok") is False and last.get("target") == target:
            return last
        return None

    async def run_schedule(self) -> None:
        """Once a day at the chosen hour: check, and update when there is a release and auto-update is on."""
        while True:
            await asyncio.sleep(600)
            try:
                data = state.load()
                if kind(data) is None:
                    continue
                settings = data["updates"]
                now = datetime.now()
                if now.hour != int(settings.get("hour", config.AUTO_UPDATE_HOUR_DEFAULT)) or \
                        self._checked_today(settings.get("last_check") or "", now):
                    continue
                result = await self.check()
                if not (result["available"] and settings.get("auto", True)):
                    continue
                failed = self._failed_before(state.load()["updates"], result.get("target") or "")
                if failed:
                    self.log.write(f"Skipping the scheduled update to {result['target']}: it failed on "
                                   f"{str(failed.get('at', ''))[:16]} ({failed.get('problem', '')}). "
                                   f"Press Update now to try again.")
                    continue
                self.start_update("scheduled")
            except Exception:  # noqa: BLE001 — the schedule must keep running
                logger.exception("update schedule tick failed")
