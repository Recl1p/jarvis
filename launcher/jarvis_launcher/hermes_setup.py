"""Make a Hermes install ready for JARVIS: the connector plugin, the API server, the browser desktop and the persona.

Every config change goes through Hermes's own ``hermes config set`` / ``hermes plugins enable`` (they keep the file's
comments and know its schema); only the API key is appended to ``.env`` directly, as Hermes documents. Safe to run again:
each step checks first and only changes what differs. Used for the Hermes JARVIS installs, and on every start to keep
the connector current.
"""

import hashlib
import logging
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger(__name__)

Log = Callable[[str], None]

# Plugins from the old JARVIS image that this one replaces (the browser now runs on Hermes's own browser tools).
RETIRED_PLUGINS = ("openjarvis-browser", "openjarvis-runtime")
RETIRED_TOOLSETS = ("openjarvis_browser", "openjarvis_runtime")
BROWSER_PLATFORMS = ("cli", "api_server", "telegram", "cron")


def hermes_env(home: Path = config.HERMES_HOME) -> dict:
    env = {**os.environ, "HERMES_HOME": str(home), "HOME": os.environ.get("HOME") or str(home),
           "NO_COLOR": "1", "TERM": "dumb", "PYTHONUNBUFFERED": "1"}
    env["PATH"] = f"{home / '.local' / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def hermes(*args: str, home: Path = config.HERMES_HOME, timeout: int = 300, binary: Optional[Path] = None) -> tuple:
    """Run the hermes CLI; returns (exit code, combined output). Never raises for a non-zero exit."""
    exe = str(binary or config.HERMES_BIN)
    try:
        done = subprocess.run([exe, *args], env=hermes_env(home), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{exe} not found"
    except subprocess.TimeoutExpired:
        return 124, f"hermes {' '.join(args)} timed out after {timeout}s"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def installed(home: Path = config.HERMES_HOME) -> bool:
    return os.access(config.HERMES_BIN, os.X_OK) and (config.HERMES_INSTALL_DIR / ".git").exists()


def config_get(key: str, home: Path = config.HERMES_HOME) -> str:
    """A scalar from config.yaml, as `hermes config get` prints it ("" when unset). Read straight from the file: a CLI
    start costs seconds, and setup checks a dozen keys. Falls back to the CLI if the file can't be parsed."""
    try:
        import yaml
        value = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 — an odd file: ask Hermes
        code, out = hermes("config", "get", key, home=home, timeout=60)
        lines = [line for line in out.strip().splitlines() if line.strip()]
        return lines[-1].strip() if code == 0 and lines else ""


def config_set(key: str, value: str, log: Log, home: Path = config.HERMES_HOME) -> None:
    if config_get(key, home) == value:
        return
    code, out = hermes("config", "set", key, value, home=home, timeout=60)
    if code != 0:
        raise RuntimeError(f"hermes config set {key} failed: {out.strip()[-400:]}")
    log(f"  set {key} = {value}")


# --- API key -----------------------------------------------------------------------------------------------------

def read_env_value(name: str, home: Path = config.HERMES_HOME) -> str:
    try:
        lines = (home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    value = ""
    for line in lines:
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip().strip("\"'")
    return value


def ensure_api_key(log: Log, home: Path = config.HERMES_HOME) -> str:
    """The Hermes API server refuses to start without a strong API_SERVER_KEY; JARVIS makes one if there is none."""
    key = read_env_value("API_SERVER_KEY", home)
    if len(key) >= 32:
        return key
    key = secrets.token_hex(32)
    env_path = home / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    kept = "\n".join(line for line in existing.splitlines() if not line.startswith("API_SERVER_KEY="))
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write((kept + "\n" if kept else "") + f"API_SERVER_KEY={key}\n")
    log("  created a new API_SERVER_KEY in .env")
    return key


# --- connector plugin ---------------------------------------------------------------------------------------------

def tree_digest(root: Path) -> str:
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                       and "tests" not in p.relative_to(root).parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def bundled_connector_version() -> str:
    return _manifest_version(config.CONNECTOR_DIR / "jarvis")


def installed_connector_version(home: Path = config.HERMES_HOME) -> str:
    return _manifest_version(home / "plugins" / "jarvis")


def _manifest_version(plugin_dir: Path) -> str:
    try:
        for line in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def connector_current(home: Path = config.HERMES_HOME) -> bool:
    return tree_digest(config.CONNECTOR_DIR / "jarvis") == tree_digest(home / "plugins" / "jarvis")


def install_connector(log: Log, home: Path = config.HERMES_HOME) -> bool:
    """Copy the bundled connector into HERMES_HOME/plugins/jarvis when it differs. Returns True when it changed."""
    source, target = config.CONNECTOR_DIR / "jarvis", home / "plugins" / "jarvis"
    if connector_current(home):
        return False
    staging = target.with_name(".jarvis.staging")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    log(f"  connector plugin {bundled_connector_version()} installed in {target}")
    return True


def retire_old_plugins(log: Log, home: Path = config.HERMES_HOME) -> None:
    for name in RETIRED_PLUGINS:
        if (home / "plugins" / name).exists():
            hermes("plugins", "disable", name, home=home, timeout=120)
            shutil.rmtree(home / "plugins" / name, ignore_errors=True)
            log(f"  removed the retired {name} plugin")
    # Read-only YAML parse to find the platforms that still list a retired toolset; the change itself goes through
    # `hermes config set` so the file keeps its comments.
    try:
        import yaml
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        cfg = {}
    platform_toolsets = cfg.get("platform_toolsets") if isinstance(cfg, dict) else None
    for platform, toolsets in (platform_toolsets or {}).items():
        if isinstance(toolsets, list) and any(t in RETIRED_TOOLSETS for t in toolsets):
            kept = [str(t) for t in toolsets if t not in RETIRED_TOOLSETS]
            code, out = hermes("config", "set", f"platform_toolsets.{platform}", "[" + ", ".join(kept) + "]",
                               home=home, timeout=60)
            if code == 0:
                log(f"  removed the retired browser toolset from {platform}")
            else:
                log(f"  could not update platform_toolsets.{platform}: {out.strip()[-200:]}")


def prepare_old_volume(log: Log, home: Path = config.HERMES_HOME) -> None:
    """Before Hermes's installer runs on a data volume from the JARVIS image before 2.0: move the retired plugins aside
    (into jarvis/retired-plugins, nothing is deleted) and drop them from plugins.enabled. The installer resolves every
    enabled plugin's dependencies, and the old browser plugin pins a Playwright with no wheels for today's Python.
    A line edit rather than a YAML rewrite, so the file keeps its comments; Hermes isn't installed yet to do it."""
    import re
    for name in RETIRED_PLUGINS:
        source = home / "plugins" / name
        if source.exists():
            parked = config.STATE_DIR / "retired-plugins" / name
            parked.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(parked, ignore_errors=True)
            shutil.move(str(source), str(parked))
            log(f"  moved the retired {name} plugin to {parked}")
    # The old image linked ~/.local/bin/hermes to its built-in Hermes (/opt/hermes); that link now points nowhere, and
    # the installer leaves an existing link alone, so the hermes command would never appear.
    for link in (home / ".local" / "bin").glob("hermes*"):
        if link.is_symlink() and os.readlink(link).startswith("/opt/hermes/"):
            link.unlink()
            log(f"  removed {link}, a link to the Hermes that was built into the old JARVIS image")
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return
    text = cfg_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^[ \t]*-[ \t]*['\"]?(?:%s)['\"]?[ \t]*\n" % "|".join(map(re.escape, RETIRED_PLUGINS)), re.M)
    cleaned = pattern.sub("", text)
    # A list left with no items would read back as null: make it an explicit empty list.
    cleaned = re.sub(r"^([ \t]*enabled:)[ \t]*\n(?![ \t]*-)", r"\1 []\n", cleaned, flags=re.M)
    if cleaned != text:
        cfg_path.write_text(cleaned, encoding="utf-8")
        log("  removed the retired plugins from plugins.enabled in config.yaml")


def enable_browser_tools(log: Log, home: Path = config.HERMES_HOME) -> None:
    """Hermes ships its browser toolset off. Turn it on for the dashboard, the CLI, Telegram and cron jobs, so they
    can drive the browser desktop (and the logins kept there)."""
    try:
        import yaml
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        cfg = {}
    platform_toolsets = (cfg.get("platform_toolsets") if isinstance(cfg, dict) else None) or {}
    for platform in BROWSER_PLATFORMS:
        toolsets = platform_toolsets.get(platform)
        if isinstance(toolsets, list) and "browser" in toolsets:
            continue
        code, out = hermes("tools", "enable", "--platform", platform, "browser", home=home, timeout=120)
        if code == 0:
            log(f"  enabled the browser tools for {platform}")
        else:
            log(f"  could not enable the browser tools for {platform}: {out.strip()[-200:]}")


def enable_connector(log: Log, home: Path = config.HERMES_HOME) -> None:
    code, out = hermes("plugins", "enable", "jarvis", home=home, timeout=600)
    if code != 0:
        raise RuntimeError(f"hermes plugins enable jarvis failed: {out.strip()[-600:]}")
    isolation = config_get("plugins.isolation", home)
    if isolation and isolation not in ("in_process", "None", "null"):
        # The connector adds routes to the API server, which only an in-process plugin can do.
        config_set("plugins.isolation", "in_process", log, home)


# --- the whole setup ----------------------------------------------------------------------------------------------

def apply(log: Log, *, managed: bool, persona: bool, home: Path = config.HERMES_HOME) -> str:
    """Configure this Hermes for JARVIS. Returns the API key the dashboard uses."""
    log("Setting up Hermes for JARVIS…")
    key = ensure_api_key(log, home)
    config_set("platforms.api_server.enabled", "true", log, home)
    if managed:
        # Loopback only: the launcher on :8642 is the way in, and it forwards to this port.
        config_set("platforms.api_server.host", "127.0.0.1", log, home)
        config_set("platforms.api_server.port", str(config.MANAGED_HERMES_PORT), log, home)
        # Hermes's own browser tools drive the browser desktop's Chromium (the noVNC view on :6080).
        config_set("browser.backend", "off", log, home)
        config_set("browser.cdp_url", "http://127.0.0.1:9222", log, home)
        config_set("browser.engine", "chrome", log, home)
        config_set("browser.headed", "true", log, home)
        workspace = home / "workspace"
        workspace.mkdir(exist_ok=True)
        config_set("terminal.cwd", str(workspace), log, home)
    retire_old_plugins(log, home)
    if managed:
        enable_browser_tools(log, home)
    install_connector(log, home)
    enable_connector(log, home)
    if persona:
        apply_persona(log, home)
    log("Hermes is set up for JARVIS.")
    return key


def apply_persona(log: Log, home: Path = config.HERMES_HOME) -> None:
    """Give Hermes the JARVIS personality on every channel (dashboard, Telegram, ...). A different SOUL.md is kept as
    SOUL.md.before-jarvis so the user can go back to it."""
    source = config.PERSONA_DIR / "SOUL.md"
    target = home / "SOUL.md"
    wanted = source.read_text(encoding="utf-8")
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if current == wanted:
        return
    if current.strip():
        backup = home / "SOUL.md.before-jarvis"
        if not backup.exists():
            backup.write_text(current, encoding="utf-8")
            log(f"  kept your previous SOUL.md as {backup.name}")
    target.write_text(wanted, encoding="utf-8")
    log("  JARVIS persona written to SOUL.md")
