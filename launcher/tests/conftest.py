"""Launcher unit tests: run with `tests/unit.sh` (or pytest with aiohttp, PyYAML and pytest-aiohttp installed).

The launcher reads its paths from the environment when it is imported, so they point at a scratch folder before any
test imports it; every test starts with that folder empty.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.mkdtemp(prefix="jarvis-launcher-tests-"))
os.environ.update({
    "HERMES_HOME": str(SCRATCH / "home"),
    "JARVIS_STATE_DIR": str(SCRATCH / "home" / "jarvis"),
    "JARVIS_HERMES_INSTALL_DIR": str(SCRATCH / "home" / "hermes-agent"),
    "JARVIS_HERMES_BIN": str(SCRATCH / "home" / ".local" / "bin" / "hermes"),
    "JARVIS_CONNECTOR_DIR": str(ROOT / "connector"),
    "JARVIS_PERSONA_DIR": str(ROOT / "persona"),
    "JARVIS_HOST_HERMES_HOME": str(SCRATCH / "host-hermes"),
})
sys.path.insert(0, str(ROOT / "launcher"))


@pytest.fixture(autouse=True)
def scratch():
    for name in ("home", "host-hermes"):
        shutil.rmtree(SCRATCH / name, ignore_errors=True)
    (SCRATCH / "home" / "jarvis").mkdir(parents=True)
    from jarvis_launcher import auth
    auth._failures.clear()
    auth.forget_basic()
    for name in ("JARVIS_AUTH", "JARVIS_PASSWORD"):
        os.environ.pop(name, None)
    yield SCRATCH
