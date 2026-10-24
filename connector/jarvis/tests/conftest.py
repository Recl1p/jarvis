"""Run the connector's tests against a stock Hermes checkout.

Copy (or symlink) this folder to ``<hermes-agent>/tests/jarvis/`` so Hermes's own conftest isolates HERMES_HOME, then
``scripts/run_tests.sh tests/jarvis``. The plugin package is imported as ``jarvis`` and its routes are appended to
``APIServerAdapter._http_route_table`` so the tests' hand-built apps serve the dashboard like a wired gateway does.
"""

import os
import sys
from pathlib import Path

import pytest

_PLUGIN_PARENT = Path(os.environ.get("JARVIS_CONNECTOR_DIR") or Path(__file__).resolve().parents[2])
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))


@pytest.fixture(autouse=True)
def _jarvis_routes(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    from jarvis import core_routes, vesper, wiring

    stock = APIServerAdapter._http_route_table

    def with_jarvis(self):
        routes = list(stock(self))
        taken = {(m, p) for m, p, _ in routes}
        extra = core_routes.http_routes(self) + vesper.http_routes(self) + wiring.connector_routes(self)
        return routes + [r for r in extra if (r[0], r[1]) not in taken]

    monkeypatch.setattr(APIServerAdapter, "_http_route_table", with_jarvis)


@pytest.fixture(autouse=True)
def _no_voice_installs(monkeypatch):
    """The Edge/Piper voices ask Hermes's package manager for their extras; tests never install anything."""
    from jarvis import vesper_voice
    monkeypatch.setattr(vesper_voice, "ensure_voice_extra", lambda engine: True)
