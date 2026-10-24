"""JARVIS connector for Hermes Agent.

Hermes stays stock: this plugin attaches the JARVIS dashboard to the Hermes API server (``ctx.register_platform_handler``
hands it the server's aiohttp app before the router freezes), keeps the Notes skill in the user's skills folder and turns
PDFs that arrive on messaging platforms into Markdown before the turn. Everything JARVIS used to patch into Hermes lives here.
"""

import logging

from .wiring import CONNECTOR_VERSION, sync_bundled_skills, wire_api_server

logger = logging.getLogger(__name__)

__all__ = ["CONNECTOR_VERSION", "register"]


def register(ctx):
    ctx.register_platform_handler("api_server", wire_api_server)
    from .pdf_ingress import convert_inbound_pdfs
    ctx.register_hook("pre_gateway_dispatch", convert_inbound_pdfs)
    try:
        sync_bundled_skills()
    except Exception:  # a read-only HERMES_HOME must not stop the dashboard from loading
        logger.warning("JARVIS: could not sync the bundled skills", exc_info=True)
