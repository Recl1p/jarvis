"""Container start, before Hermes's boot reconciler starts the gateway (image/cont-init.d/20-hermes-s6).

Copies this image's connector into a managed Hermes, so the gateway starts with the connector the image ships instead
of the one from before an image update. File copies only: Hermes's CLI isn't needed, and the launcher re-applies the
rest of the setup once it runs.
"""

import sys

from . import hermes_setup, state


def main() -> int:
    if state.load().get("mode") != "managed" or not hermes_setup.installed():
        return 0
    try:
        hermes_setup.install_connector(lambda line: print(f"[jarvis]{line}"))
    except OSError as exc:
        print(f"[jarvis] could not update the connector before start: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
