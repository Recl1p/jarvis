#!/usr/bin/env bash
# Launcher unit tests, in a throwaway Python container (nothing to install on this machine).
#   tests/unit.sh                 DOCKER="sudo -n docker" tests/unit.sh
# The connector's tests need a Hermes checkout: see connector/jarvis/tests/conftest.py.
set -euo pipefail
D=${DOCKER:-docker}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
$D run --rm -v "$ROOT:/src:ro" -w /src/launcher/tests python:3.13-slim sh -c '
  pip install --quiet --disable-pip-version-check --root-user-action=ignore \
      aiohttp==3.11.18 pyyaml==6.0.2 pytest==8.3.5 pytest-aiohttp==1.1.0 pytest-asyncio==0.26.0 &&
  python -m pytest -q -p no:cacheprovider "$@"' sh "$@"
