"""PDFs sent on messaging platforms (Telegram and the like) are converted to Markdown before the turn.

A ``pre_gateway_dispatch`` hook: each attached PDF becomes a sibling ``.md`` (pdf_markdown.py, off the event loop since a
large book takes seconds) and the message text gets a note with the Markdown inlined up to ``PDF_MARKDOWN_INLINE_CHARS``,
so the agent reads the document instead of asking the user to paste it. Hermes still adds its own note pointing at the
original file; a PDF that can't be converted keeps only that one.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

PDF_MARKDOWN_INLINE_CHARS = 60_000


def pdf_markdown_context_note(display_name: str, pdf_path: str, md_path: str, markdown: str,
                              *, inline_chars: int = PDF_MARKDOWN_INLINE_CHARS) -> str:
    """Inlines the Markdown up to ``inline_chars``; a longer conversion points at the .md for the rest."""
    truncated = len(markdown) > inline_chars
    coverage = (
        f"The first {inline_chars:,} of {len(markdown):,} characters are included below; read the "
        f"rest of the .md file yourself (read_file) when the user's request involves later parts."
        if truncated else "The full Markdown is included below.")
    return (
        f"[The user sent a PDF: '{display_name}'. It was converted to Markdown before this turn and "
        f"saved at: {md_path} (original PDF: {pdf_path}). Analyze the Markdown conversion to answer, "
        f"instead of asking the user to paste the contents. {coverage}]\n\n"
        f"{markdown[:inline_chars].rstrip()}" + ("\n\n[…truncated…]" if truncated else ""))


async def convert_inbound_pdfs(event=None, gateway=None, session_store=None, **_):
    media = list(getattr(event, "media_urls", None) or [])
    if not media:
        return None
    from gateway.run import _event_media_is_audio, _event_media_is_image, _event_media_is_video
    from gateway.run_inbound import GatewayInboundMixin
    from tools.credential_files import to_agent_visible_cache_path

    from .pdf_markdown import convert_pdf_file_to_markdown_file, is_pdf
    types = list(getattr(event, "media_types", None) or [])
    notes = []
    for i, path in enumerate(media):
        mtype = types[i] if i < len(types) else ""
        if not (is_pdf(path) or mtype == "application/pdf"):
            continue
        if any(f(event, i) for f in (_event_media_is_image, _event_media_is_audio, _event_media_is_video)):
            continue
        display_name, agent_path = GatewayInboundMixin._inbound_attachment_display_name(path)
        try:
            markdown, md_path = await asyncio.to_thread(convert_pdf_file_to_markdown_file, path, display_name)
        except Exception as exc:  # noqa: BLE001 — the stock document note still points at the PDF
            logger.warning("PDF-to-Markdown conversion failed for %s: %s", path, exc)
            continue
        notes.append(pdf_markdown_context_note(display_name, agent_path, to_agent_visible_cache_path(md_path), markdown))
    if not notes:
        return None
    return {"action": "rewrite", "text": "\n\n".join(notes + [str(getattr(event, "text", "") or "")])}
