#!/usr/bin/env bash
# The connector's tests (connector/jarvis/tests), against a stock Hermes installed by its official installer
# (the image tests/e2e/hermes-stock, built here if missing).
#   tests/connector.sh [pytest args]            DOCKER="sudo -n docker" tests/connector.sh -k notes
set -euo pipefail
D=${DOCKER:-docker}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=localhost/hermes-stock:e2e
$D image inspect "$IMAGE" >/dev/null 2>&1 || $D build -t "$IMAGE" "$ROOT/tests/e2e/hermes-stock"
$D run --rm --entrypoint bash -v "$ROOT/connector:/connector:ro" "$IMAGE" -c '
  set -e
  H=$HOME/.hermes/hermes-agent
  UV=$(ls -d $HOME/.hermes/tools/uv-*/uv | head -1)
  PY=$(ls -d $HOME/.hermes/tools/python-*/bin/python3 | head -1)
  $UV pip install --quiet --target /tmp/pt --python "$PY" pytest==9.1.1 pytest-asyncio==1.3.0 pyyaml==6.0.2 >/dev/null
  cp -r /connector/jarvis/tests "$H/tests/jarvis"
  printf "import sys\nsys.path.insert(0, \"/tmp/pt\")\nimport pytest\nsys.exit(pytest.main(sys.argv[1:]))\n" > "$H/jarvis_pytest_shim.py"
  cd "$H" && JARVIS_CONNECTOR_DIR=/connector PYTHONDONTWRITEBYTECODE=1 \
      hermes --run-module jarvis_pytest_shim tests/jarvis -q -p no:cacheprovider -W ignore::DeprecationWarning "$@"' bash "$@"
