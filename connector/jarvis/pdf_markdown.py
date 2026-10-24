"""Always-on PDF -> Markdown conversion for chat attachments.

Every PDF a user attaches to JARVIS -- the dashboard composer's attach button or a document sent
over a messaging platform -- is converted to a Markdown document and saved as a ``.md`` file
*before* the agent analyzes it, so the model reasons over structured Markdown (headings, lists,
tables) instead of raw PDF bytes or an "extract the text yourself" hint.

Conversion goes through ``tools.read_extract`` (anydoc's PDF->Markdown, including its scanned-page
recovery). When that cannot handle the file at all -- anydoc unavailable, or it rejects a PDF that
poppler can still read -- per-page ``pdftotext`` output is used instead, one ``## Page N`` heading
per page, so a readable PDF always yields Markdown rather than a failed attachment.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from tools import read_extract

# Dashboard conversions have no owning cache entry, so they are pruned by age on each new save.
# Long enough that a follow-up turn days later can still read_file the Markdown it was pointed at.
PDF_MARKDOWN_RETENTION_SECONDS = 30 * 24 * 3600
_SAFE_STEM_MAX_LEN = 120
_ENTRY_DIR_RE = re.compile(r"[0-9a-f]{32}")


def is_pdf(name: str) -> bool:
    return Path(name).suffix.lower() == ".pdf"


def pdf_markdown_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "attachments" / "pdf-markdown"


def _safe_stem(filename: str) -> str:
    stem = Path(os.path.basename(filename)).stem
    return (re.sub(r"[^\w.\- ]", "_", stem).strip(" .") or "attachment")[:_SAFE_STEM_MAX_LEN]


def _bytes_page_texts(data: bytes) -> Optional[list]:
    """Per-page pdftotext output for in-memory PDF bytes, via a temp copy (pdftotext needs a path)."""
    with contextlib.suppress(OSError), read_extract._temp_copy(data, ".pdf") as temp_path:
        return read_extract._pdf_page_texts(temp_path)
    return None


def _pages_markdown(texts: list) -> str:
    return "\n\n".join(
        f"## Page {i}\n\n{text.strip() or '_(no text layer on this page)_'}"
        for i, text in enumerate(texts, start=1))


def _markdown_body(convert: Callable[[], str], page_texts: Callable[[], Optional[list]]) -> str:
    try:
        body = convert()
    except read_extract.ExtractionError:
        texts = page_texts()
        if not texts or not any(text.strip() for text in texts):
            raise
        return _pages_markdown(texts)
    # Hermes answers a PDF with some scanned pages with only its "[NEEDS OCR: ...]" note (its converter is all or
    # nothing). Keep the note, and add what pdftotext still reads from every page that has a text layer.
    if body.lstrip().startswith("[NEEDS OCR:") and body.strip().endswith("]"):
        texts = page_texts()
        if texts and any(text.strip() for text in texts):
            return body.strip() + "\n\n" + _pages_markdown(texts)
    return body


def markdown_document(filename: str, body: str) -> str:
    title = os.path.basename(filename) or "attachment.pdf"
    return (f"# {title}\n\n"
            "> Converted from PDF to Markdown by JARVIS before analysis.\n\n"
            f"{body.strip()}\n")


def pdf_bytes_to_markdown(data: bytes, filename: str) -> str:
    """Markdown for in-memory PDF bytes; raises ExtractionError when nothing is readable."""
    body = _markdown_body(lambda: read_extract.extract_document_bytes(data, filename),
                          lambda: _bytes_page_texts(data))
    return markdown_document(filename, body)


def pdf_file_to_markdown(path: str, display_name: Optional[str] = None) -> str:
    """Markdown for a PDF on disk; raises ExtractionError when nothing is readable."""
    body = _markdown_body(lambda: read_extract.extract_document_text(path),
                          lambda: read_extract._pdf_page_texts(path))
    return markdown_document(display_name or path, body)


def _prune(base: Path, now: Optional[float] = None) -> None:
    cutoff = (time.time() if now is None else now) - PDF_MARKDOWN_RETENTION_SECONDS
    with contextlib.suppress(OSError):
        for entry in base.iterdir():
            if (entry.is_dir() and _ENTRY_DIR_RE.fullmatch(entry.name)
                    and entry.stat().st_mtime < cutoff):
                shutil.rmtree(entry, ignore_errors=True)


def save_markdown(markdown: str, filename: str, directory: Optional[Path] = None) -> Path:
    """Write ``markdown`` to ``<dir>/<32-hex>/<sanitized stem>.md``. The client-supplied filename
    only contributes a sanitized stem, never a directory component."""
    base = directory or pdf_markdown_dir()
    _prune(base)
    target_dir = base / uuid.uuid4().hex
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_safe_stem(filename)}.md"
    target.write_text(markdown, encoding="utf-8")
    return target


def markdown_sibling_path(pdf_path: str) -> str:
    return os.path.splitext(pdf_path)[0] + ".md"


def convert_pdf_file_to_markdown_file(path: str, display_name: Optional[str] = None) -> tuple[str, str]:
    """Convert a cached PDF to a sibling ``.md`` (same cache lifetime as the PDF) and return
    ``(markdown, md_path)``. An up-to-date sibling is reused, since queued follow-ups re-run
    inbound preprocessing for the same file."""
    md_path = markdown_sibling_path(path)
    with contextlib.suppress(OSError):
        if os.path.getmtime(md_path) >= os.path.getmtime(path):
            return Path(md_path).read_text(encoding="utf-8"), md_path
    markdown = pdf_file_to_markdown(path, display_name)
    Path(md_path).write_text(markdown, encoding="utf-8")
    return markdown, md_path
