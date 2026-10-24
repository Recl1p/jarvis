"""Paths and settings, all overridable from the environment (compose.yaml sets the container's)."""

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default)


# Hermes's own home. In the container it is the data volume, so an installed Hermes survives image updates.
HERMES_HOME = _path("HERMES_HOME", "/opt/data")
HERMES_INSTALL_DIR = _path("JARVIS_HERMES_INSTALL_DIR", str(HERMES_HOME / "hermes-agent"))
HERMES_BIN = _path("JARVIS_HERMES_BIN", str(HERMES_HOME / ".local" / "bin" / "hermes"))
# The launcher's own state (connection, consent, update history), next to Hermes's data.
STATE_DIR = _path("JARVIS_STATE_DIR", str(HERMES_HOME / "jarvis"))
# Shipped in the image: the connector plugin(s) and the JARVIS persona.
CONNECTOR_DIR = _path("JARVIS_CONNECTOR_DIR", "/opt/jarvis/connector")
PERSONA_DIR = _path("JARVIS_PERSONA_DIR", "/opt/jarvis/persona")
STATIC_DIR = Path(__file__).resolve().parent / "static"
# A host's ~/.hermes, when the user mounts it so JARVIS can find that Hermes (compose.yaml, commented out).
HOST_HERMES_HOME = _path("JARVIS_HOST_HERMES_HOME", "/hermes-host")

LISTEN_HOST = os.environ.get("JARVIS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("JARVIS_PORT", "8642"))
# Where a managed Hermes's API server listens: loopback only, the launcher is the way in.
MANAGED_HERMES_PORT = int(os.environ.get("JARVIS_HERMES_PORT", "8643"))
MANAGED_HERMES_URL = f"http://127.0.0.1:{MANAGED_HERMES_PORT}"
# OmniVoice, installed on request from the setup screen (voices.py) into the data volume, with the image's own pins.
OMNIVOICE_DIR = _path("JARVIS_OMNIVOICE_DIR", str(HERMES_HOME / "omnivoice"))
OMNIVOICE_IMAGE_DIR = _path("JARVIS_OMNIVOICE_IMAGE_DIR", "/opt/omnivoice")
OMNIVOICE_REQUIREMENTS = _path("JARVIS_OMNIVOICE_REQUIREMENTS", "/opt/omnivoice/requirements.txt")
UV = os.environ.get("JARVIS_UV", "/usr/local/bin/uv")
# The browser desktop's noVNC server (desktop/start.sh), which the launcher serves at /desktop/ behind the login.
DESKTOP_URL = os.environ.get("JARVIS_DESKTOP_URL", "http://127.0.0.1:6080")

# The official Hermes installer. JARVIS never ships Hermes: with the user's consent it downloads and runs this.
HERMES_INSTALLER_URL = os.environ.get("JARVIS_HERMES_INSTALLER_URL", "https://hermes-agent.nousresearch.com/install.sh")
HERMES_REPO_URL = "https://github.com/NousResearch/hermes-agent"
HERMES_LICENSE_URL = "https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"
HERMES_CHANNEL = os.environ.get("JARVIS_HERMES_CHANNEL", "stable")
# Optional exact commit to install (empty = the channel's latest release).
HERMES_COMMIT = os.environ.get("JARVIS_HERMES_COMMIT", "")
# The oldest Hermes this connector is tested with (a newer one is checked live after every update).
HERMES_MIN_VERSION = "0.22.0"

# Extra subnets to scan for a Hermes when "I already have Hermes" is chosen, e.g. "192.168.1.0/24".
SCAN_SUBNETS = [s.strip() for s in os.environ.get("JARVIS_SCAN_SUBNETS", "").split(",") if s.strip()]
# Extra Hermes URLs to try first, e.g. "http://192.168.1.20:8642".
HERMES_CANDIDATES = [s.strip() for s in os.environ.get("JARVIS_HERMES_URLS", "").split(",") if s.strip()]

# Auto-update defaults (the user changes them in the setup screen; the saved choice wins).
AUTO_UPDATE_DEFAULT = os.environ.get("JARVIS_AUTO_UPDATE", "1") not in ("0", "false", "no")
AUTO_UPDATE_HOUR_DEFAULT = int(os.environ.get("JARVIS_AUTO_UPDATE_HOUR", "4"))

# How long a freshly started or updated Hermes gets to answer /health with the connector loaded.
HEALTH_TIMEOUT_SECONDS = int(os.environ.get("JARVIS_HEALTH_TIMEOUT", "240"))
