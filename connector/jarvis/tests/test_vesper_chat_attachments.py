"""Chat attachment document-extraction endpoint (dashboard composer "attach file" feature).

Plain text/code and image attachments never reach the backend as a distinct code path — the
dashboard inlines them client-side as ordinary {"type":"text"} / {"type":"image_url"} content
parts, which _normalize_multimodal_content already accepted before this feature (see
test_api_server_multimodal.py). This file covers the one genuinely new server surface: extracting
readable text from document formats (PDF, Word, PowerPoint, Excel, OpenDocument, RTF, EPUB,
Jupyter notebooks) that need a real converter before they're useful in a chat message.
"""

import io
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
EXTRACT_PATH = "/vesper/chat/extract-attachment"


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def _minimal_docx_bytes(paragraph_text: str) -> bytes:
    """The smallest word/document.xml package _extract_docx (tools/read_extract.py) can read."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>' + paragraph_text + '</w:t></w:r></w:p></w:body>'
        '</w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


class TestExtractChatAttachment:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        async with TestClient(TestServer(app())) as client:
            resp = await client.post(
                EXTRACT_PATH, data=b"irrelevant",
                headers={"X-Attachment-Filename": "notes.pdf"})
            assert resp.status in (401, 403)

    @pytest.mark.asyncio
    async def test_missing_filename_header_rejected(self):
        async with TestClient(TestServer(app())) as client:
            resp = await client.post(EXTRACT_PATH, data=b"irrelevant", headers=AUTH)
            assert resp.status == 400
            body = await resp.json()
            assert body["error"]["code"] == "missing_filename"

    @pytest.mark.asyncio
    async def test_unsupported_extension_rejected(self):
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "notes.txt"}
            resp = await client.post(EXTRACT_PATH, data=b"hello world", headers=headers)
            assert resp.status == 415
            body = await resp.json()
            assert body["error"]["code"] == "unsupported_document_type"

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self):
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "notes.pdf"}
            resp = await client.post(EXTRACT_PATH, data=b"", headers=headers)
            assert resp.status == 400
            body = await resp.json()
            assert body["error"]["code"] == "empty_body"

    @pytest.mark.asyncio
    async def test_docx_extracts_text(self):
        data = _minimal_docx_bytes("Hello from a real DOCX attachment.")
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "My%20Notes.docx"}
            resp = await client.post(EXTRACT_PATH, data=data, headers=headers)
            assert resp.status == 200, await resp.text()
            payload = await resp.json()
            assert payload["filename"] == "My Notes.docx"
            assert "Hello from a real DOCX attachment." in payload["text"]
            assert payload["truncated"] is False

    @pytest.mark.asyncio
    async def test_malformed_document_reports_extraction_error(self):
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "broken.docx"}
            resp = await client.post(EXTRACT_PATH, data=b"not a real zip", headers=headers)
            assert resp.status == 422
            body = await resp.json()
            assert body["error"]["code"] == "extraction_failed"

    @pytest.mark.asyncio
    async def test_filename_is_sanitized_to_basename(self):
        """A path-separator-bearing filename never leaks a directory component back to the
        client, and is never used to touch the filesystem (extraction operates on the uploaded
        bytes only, via the extractor's own private tempfile)."""
        data = _minimal_docx_bytes("Path traversal guard check.")
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "..%2F..%2Fetc%2Fpasswd.docx"}
            resp = await client.post(EXTRACT_PATH, data=data, headers=headers)
            assert resp.status == 200, await resp.text()
            payload = await resp.json()
            assert "/" not in payload["filename"]
            assert payload["filename"] == "passwd.docx"

    @pytest.mark.asyncio
    async def test_docx_response_has_no_markdown_file(self):
        data = _minimal_docx_bytes("Not a PDF.")
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "notes.docx"}
            resp = await client.post(EXTRACT_PATH, data=data, headers=headers)
            payload = await resp.json()
            assert "markdown_path" not in payload
            assert "format" not in payload

    @pytest.mark.asyncio
    async def test_pdf_is_always_converted_to_a_markdown_file(self, tmp_path, monkeypatch):
        """Every PDF becomes Markdown before the agent sees it: the response text is the Markdown
        conversion and the full .md is saved server-side for read_file."""
        from jarvis import pdf_markdown
        from tools import read_extract
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(read_extract, "extract_document_bytes",
                            lambda data, path: "## Chapter 1\n\nReal chapter text.\n")
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "..%2FText%20Book.pdf"}
            resp = await client.post(EXTRACT_PATH, data=b"%PDF-1.4 fake", headers=headers)
            assert resp.status == 200, await resp.text()
            payload = await resp.json()
        assert payload["format"] == "markdown"
        assert payload["text"].startswith("# Text Book.pdf\n\n")
        assert "## Chapter 1" in payload["text"]
        md_path = payload["markdown_path"]
        assert md_path.endswith("/Text Book.md")
        assert md_path.startswith(str(pdf_markdown.pdf_markdown_dir()))
        with open(md_path, encoding="utf-8") as fh:
            assert fh.read() == payload["text"]

    @pytest.mark.asyncio
    async def test_truncated_pdf_markdown_file_keeps_full_text(self, tmp_path, monkeypatch):
        from jarvis import vesper
        from tools import read_extract
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(vesper, "_ATTACHMENT_EXTRACT_TEXT_CAP", 50)
        monkeypatch.setattr(read_extract, "extract_document_bytes", lambda data, path: "z" * 400)
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "long.pdf"}
            payload = await (await client.post(EXTRACT_PATH, data=b"%PDF", headers=headers)).json()
        assert payload["truncated"] is True
        assert len(payload["text"]) == 50
        with open(payload["markdown_path"], encoding="utf-8") as fh:
            assert fh.read().count("z") == 400

    @pytest.mark.asyncio
    async def test_pdf_accepted_without_anydoc_via_pdftotext_fallback(self, tmp_path, monkeypatch):
        from jarvis import pdf_markdown
        from tools import read_extract
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(read_extract, "_anydoc", lambda: None)
        monkeypatch.setattr(pdf_markdown, "_bytes_page_texts", lambda data: ["Poppler page one"])
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "plain.pdf"}
            resp = await client.post(EXTRACT_PATH, data=b"%PDF-1.4", headers=headers)
            assert resp.status == 200, await resp.text()
            payload = await resp.json()
        assert "## Page 1\n\nPoppler page one" in payload["text"]
        assert payload["format"] == "markdown"

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self, monkeypatch):
        from jarvis import vesper
        monkeypatch.setattr(vesper, "CHAT_ATTACHMENT_EXTRACT_MAX_BYTES", 10)
        async with TestClient(TestServer(app())) as client:
            headers = {**AUTH, "X-Attachment-Filename": "notes.pdf"}
            resp = await client.post(EXTRACT_PATH, data=b"x" * 1000, headers=headers)
            assert resp.status == 413
            body = await resp.json()
            assert body["error"]["code"] == "attachment_too_large"
