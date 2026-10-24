"""VESPER agent controls over the existing profile-scoped Hermes cron store."""

import asyncio
import contextlib
import json
import os
import re
import tempfile
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from aiohttp import web


def http_routes(adapter):
    from .sessions_meta import http_routes as sessions_meta_routes
    from .setup_guide import http_routes as setup_routes
    from .setup_claude import http_routes as claude_setup_routes
    from .setup_providers import http_routes as provider_setup_routes
    from .setup_telegram import http_routes as telegram_setup_routes
    from .setup_voice import http_routes as voice_setup_routes
    from .vesper_app import http_routes as app_routes
    from .vesper_prefs import http_routes as prefs_routes
    from .vesper_voice import http_routes as voice_routes

    return [
        ("GET", "/vesper/storage.js", static_script("storage.js")),
        ("GET", "/vesper/agents.js", static_script("agents.js")),
        ("GET", "/vesper/cloudflare-speedtest.js", static_script("cloudflare-speedtest.js")),
        ("GET", "/vesper/tour.js", static_script("tour.js")),
        ("GET", "/vesper/sessions.js", static_script("sessions.js")),
        ("GET", "/vesper/personas.js", static_script("personas.js")),
        ("GET", "/vesper/core.js", static_script("core.js")),
        # React, ReactDOM and Babel for support.js, pinned (assets/vesper/vendor), so the dashboard needs no CDN.
        ("GET", "/vesper/vendor/react.production.min.js", static_script("vendor/react.production.min.js", VENDOR_CACHE)),
        ("GET", "/vesper/vendor/react-dom.production.min.js", static_script("vendor/react-dom.production.min.js", VENDOR_CACHE)),
        ("GET", "/vesper/vendor/babel.min.js", static_script("vendor/babel.min.js", VENDOR_CACHE)),
        ("POST", "/vesper/agents", partial(save_agent, adapter)),
        ("PATCH", "/vesper/agents/{job_id}", partial(save_agent, adapter)),
        ("GET", "/vesper/agents/{job_id}/output", partial(agent_output, adapter)),
        ("GET", "/vesper/music/tracks", partial(list_music, adapter)),
        ("POST", "/vesper/music/tracks", partial(upload_music, adapter)),
        ("GET", "/vesper/music/tracks/{track_id}", stream_music),
        ("PATCH", "/vesper/music/tracks/{track_id}", partial(rename_music, adapter)),
        ("DELETE", "/vesper/music/tracks/{track_id}", partial(delete_music, adapter)),
        ("GET", "/vesper/notes", partial(list_notes, adapter)),
        ("POST", "/vesper/notes", partial(create_note, adapter)),
        ("GET", "/vesper/notes/{note_id}", partial(get_note, adapter)),
        ("PATCH", "/vesper/notes/{note_id}", partial(patch_note, adapter)),
        ("DELETE", "/vesper/notes/{note_id}", partial(delete_note, adapter)),
        ("GET", "/vesper/location", partial(get_location, adapter)),
        ("PUT", "/vesper/location", partial(put_location, adapter)),
        ("DELETE", "/vesper/location", partial(delete_location, adapter)),
        ("GET", "/vesper/geocode", partial(geocode, adapter)),
        ("POST", "/vesper/chat/extract-attachment", partial(extract_chat_attachment, adapter)),
    ] + (setup_routes(adapter) + provider_setup_routes(adapter) + claude_setup_routes(adapter) + sessions_meta_routes(adapter)
        + prefs_routes(adapter) + app_routes(adapter) + voice_routes(adapter) + voice_setup_routes(adapter)
        + telegram_setup_routes(adapter))


def static_script(filename, cache="no-cache"):
    """Unauthenticated handler for a dashboard script in gateway/assets/vesper. The file is read
    per request, so an edited asset is served without a gateway restart."""
    path = Path(__file__).resolve().parent / "assets" / "vesper" / filename

    async def serve(request):
        return web.Response(text=path.read_text(encoding="utf-8"),
                            content_type="application/javascript", headers={"Cache-Control": cache})

    return serve


# The vendored libraries never change under the same name (a new version is a new file and an SRI hash in support.js).
VENDOR_CACHE = "public, max-age=31536000, immutable"


def validated_fields(body, adapter):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    fields = {}
    for name, limit in (("name", adapter._MAX_NAME_LENGTH),
                        ("prompt", adapter._MAX_PROMPT_LENGTH), ("schedule", 200)):
        value = body.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} is required and must be at most {limit} characters")
        fields[name] = value.strip()
    for name in ("model", "provider"):
        value = body.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ValueError(f"Invalid {name}")
        fields[name] = value.strip() or None if value is not None else None
    for name in ("skills", "enabled_toolsets"):
        value = body.get(name)
        if value is not None:
            if (not isinstance(value, list) or len(value) > 100
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in value)):
                raise ValueError(f"{name} must be a list of names")
            value = list(dict.fromkeys(v.strip() for v in value))
            # Hermes treats [] as the default toolset, so never promise 'no tools'.
            if name == "enabled_toolsets" and not value:
                raise ValueError("Select at least one toolset, or use Hermes defaults")
        fields[name] = value
    return fields


async def save_agent(adapter, request):
    from gateway.platforms import api_server

    editing = request.method == "PATCH"
    job_id, error = adapter._cron_request_guard(request, need_job_id=editing, check_draining=True)
    if error is not None:
        return error
    try:
        fields = validated_fields(await request.json(), adapter)
        error = adapter._validate_cron_prompt(fields["prompt"])
        if error is not None:
            return error
        if editing:
            job = await asyncio.to_thread(api_server._cron_update, job_id, fields)
            if job is None:
                return web.json_response({"error": "Job not found"}, status=404)
        else:
            job = await asyncio.to_thread(
                api_server._cron_create, **fields, deliver="local",
                origin=adapter._cron_origin_from_request(request))
        api_server._notify_cron_provider_jobs_changed()
        return web.json_response({"job": job}, status=200 if editing else 201)
    except api_server._CronSchedulerRegistrationError as exc:
        return web.json_response(exc.to_dict(), status=424)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        return adapter._cron_error_response(exc)


def read_output(job_id, filename=None):
    from cron.jobs import _job_output_dir

    directory = _job_output_dir(job_id)
    if directory.is_symlink():
        raise ValueError("Invalid output directory")
    files = sorted((p for p in directory.glob("*.md") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.name, reverse=True)[:20]
    names = [p.name for p in files]
    if filename and filename not in names:
        raise FileNotFoundError("Output not found")
    selected = filename or (names[0] if names else None)
    content, truncated = "", False
    if selected:
        with (directory / selected).open("rb") as stream:
            data = stream.read(256_001)
        truncated = len(data) > 256_000
        content = data[:256_000].decode("utf-8", errors="replace")
    return {"files": names, "selected": selected, "content": content, "truncated": truncated}


async def agent_output(adapter, request):
    from gateway.platforms import api_server

    job_id, error = adapter._cron_request_guard(request, need_job_id=True)
    if error is not None:
        return error
    try:
        if await asyncio.to_thread(api_server._cron_get, job_id) is None:
            return web.json_response({"error": "Job not found"}, status=404)
        data = await asyncio.to_thread(read_output, job_id, request.query.get("file"))
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return web.json_response({"error": "Output not found"}, status=404)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        return adapter._cron_error_response(exc)


# --- Custom music upload (VESPER dashboard "MY MUSIC") -----------------------------
#
# Uploaded files live at get_hermes_home()/vesper-music, on the same persistent volume as
# config.yaml/auth.json. There is no separate metadata store: a track's id, title and
# extension are all encoded in its filename as ``{uuid_hex}__{percent_encoded_title}.{ext}``,
# and size/upload time come from os.stat(). Parsing that filename back is positional (fixed
# 32-hex-char id, literal "__", encoded title, extension) rather than a naive split on "__" so
# a title that itself contains "__" can never corrupt parsing. The GET (stream) route is
# deliberately unauthenticated -- like /vesper/radio and /vesper/files -- because a bare
# <audio src> can't carry an Authorization header; containment instead comes from validating
# the id as exactly 32 hex characters before ever touching the filesystem, then resolving it
# only inside this one fixed directory (never an arbitrary path).

_MUSIC_EXTENSIONS = {"mp3", "wav", "ogg", "m4a", "flac", "aac", "opus"}
_MUSIC_MIME = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "m4a": "audio/mp4",
    "flac": "audio/flac", "aac": "audio/aac", "opus": "audio/opus"}
_MUSIC_MAX_BYTES = 100 * 1024 * 1024
# Raw document bytes for /vesper/chat/extract-attachment (the extracted text sent back is capped separately).
CHAT_ATTACHMENT_EXTRACT_MAX_BYTES = 200 * 1024 * 1024 + 4096
_MUSIC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MUSIC_TITLE_MAX_LEN = 120


def _music_dir() -> Path:
    from hermes_constants import get_hermes_home
    directory = get_hermes_home() / "vesper-music"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _parse_music_filename(name):
    """Positional parse of ``{32-hex-id}__{percent-encoded-title}.{ext}``; None if it
    doesn't match (e.g. a stray temp file from an in-progress upload)."""
    if len(name) < 32 + 2 + 1 + 1 or name[32:34] != "__":
        return None
    music_id = name[:32]
    if not _MUSIC_ID_RE.match(music_id):
        return None
    rest = name[34:]
    dot = rest.rfind(".")
    if dot <= 0:
        return None
    ext = rest[dot + 1:].lower()
    if ext not in _MUSIC_EXTENSIONS:
        return None
    try:
        title = urllib.parse.unquote(rest[:dot])
    except Exception:
        return None
    return music_id, title, ext


def _find_music_path(music_id):
    """None unless *music_id* is a well-formed id AND a matching file exists."""
    if not music_id or not _MUSIC_ID_RE.match(music_id):
        return None
    matches = sorted(_music_dir().glob(f"{music_id}__*"))
    return matches[0] if matches else None


def _list_music_entries():
    entries = []
    for path in _music_dir().iterdir():
        if not path.is_file():
            continue
        parsed = _parse_music_filename(path.name)
        if not parsed:
            continue
        music_id, title, ext = parsed
        stat = path.stat()
        entries.append({"id": music_id, "title": title, "ext": ext,
                         "size_bytes": stat.st_size, "uploaded_at": stat.st_mtime})
    entries.sort(key=lambda e: e["uploaded_at"])
    return entries


def _track_dict(music_id, title, ext, stat):
    return {"id": music_id, "title": title, "ext": ext,
            "size_bytes": stat.st_size, "uploaded_at": stat.st_mtime}


async def list_music(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response({"tracks": await asyncio.to_thread(_list_music_entries)})


async def upload_music(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    from gateway.platforms import api_server

    raw_name = request.headers.get("X-Music-Filename", "").strip()
    if not raw_name:
        return api_server._error_response(
            "X-Music-Filename header is required.", 400, code="missing_filename")
    try:
        filename = urllib.parse.unquote(raw_name)
    except Exception:
        return api_server._error_response(
            "Invalid X-Music-Filename encoding.", 400, code="invalid_filename")
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in _MUSIC_EXTENSIONS:
        return api_server._error_response(
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(_MUSIC_EXTENSIONS))}",
            415, code="unsupported_music_type")
    title = Path(filename).stem.strip()[:_MUSIC_TITLE_MAX_LEN] or "Untitled"
    try:
        # NOT request.content.read(n): StreamReader.read(n) only waits for the *first*
        # buffered chunk to arrive, then returns whatever's currently buffered (up to n) --
        # it does not accumulate until n bytes or EOF. Verified directly: a 15MB body sent
        # over loopback came back as exactly 65536 bytes (one asyncio transport read) when
        # read this way. Looping on readany() until EOF (or the cap) is the correct
        # bounded-accumulation pattern; this bypasses aiohttp's app-level client_max_size
        # (10MB) the same way a single read() call would, since client_max_size is only
        # enforced inside request.read()/.json()/.multipart(), never the raw content stream.
        body = bytearray()
        async for chunk in request.content.iter_any():
            body.extend(chunk)
            if len(body) > _MUSIC_MAX_BYTES:
                break
        data = bytes(body)
    except Exception:
        return api_server._error_response("Failed to read request body.", 400, code="read_failed")
    if len(data) > _MUSIC_MAX_BYTES:
        return api_server._error_response(
            f"File exceeds the {_MUSIC_MAX_BYTES}-byte cap.", 413, code="music_too_large")
    if not data:
        return api_server._error_response("Empty file body.", 400, code="empty_body")

    def _write():
        directory = _music_dir()
        music_id = uuid.uuid4().hex
        encoded_title = urllib.parse.quote(title, safe="")
        final_path = directory / f"{music_id}__{encoded_title}.{ext}"
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, final_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return music_id, final_path.stat()

    music_id, stat = await asyncio.to_thread(_write)
    return web.json_response({"track": _track_dict(music_id, title, ext, stat)}, status=201)


async def stream_music(request):
    from gateway.platforms import api_server

    track_id = request.match_info.get("track_id", "")
    path = await asyncio.to_thread(_find_music_path, track_id)
    if path is None:
        return api_server._error_response("Track not found", 404, code="music_not_found")
    parsed = _parse_music_filename(path.name)
    content_type = _MUSIC_MIME.get(parsed[2] if parsed else "", "application/octet-stream")
    return web.FileResponse(path, headers={"Content-Type": content_type})


async def rename_music(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    from gateway.platforms import api_server

    track_id = request.match_info.get("track_id", "")
    path = await asyncio.to_thread(_find_music_path, track_id)
    if path is None:
        return api_server._error_response("Track not found", 404, code="music_not_found")
    try:
        body = await request.json()
    except Exception:
        return api_server._error_response("Invalid JSON body", 400, code="invalid_json")
    title = str((body or {}).get("title", "")).strip()[:_MUSIC_TITLE_MAX_LEN]
    if not title:
        return api_server._error_response("title is required", 400, code="missing_title")
    ext = _parse_music_filename(path.name)[2]

    def _rename():
        encoded_title = urllib.parse.quote(title, safe="")
        new_path = path.with_name(f"{track_id}__{encoded_title}.{ext}")
        os.replace(path, new_path)
        return new_path.stat()

    stat = await asyncio.to_thread(_rename)
    return web.json_response({"track": _track_dict(track_id, title, ext, stat)})


async def delete_music(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    from gateway.platforms import api_server

    track_id = request.match_info.get("track_id", "")
    path = await asyncio.to_thread(_find_music_path, track_id)
    if path is None:
        return api_server._error_response("Track not found", 404, code="music_not_found")
    await asyncio.to_thread(os.unlink, path)
    return web.json_response({"deleted": True})


# --- Chat attachment extraction (VESPER dashboard composer "attach file") ---------------
#
# Plain text/code files and images never touch this endpoint: the dashboard reads text files
# with FileReader.readAsText and inlines them as an OpenAI-style {"type":"text"} content part,
# and images with FileReader.readAsDataURL as an {"type":"image_url"} part — both shapes
# _normalize_multimodal_content() (api_server.py) already accepted before this feature existed,
# so no backend change was needed for either. This endpoint exists only for formats that need a
# real document-to-text converter before they're useful in a chat message: PDF, Word/PowerPoint/
# Excel (old and new), OpenDocument, RTF, EPUB, Jupyter notebooks.
#
# Design choice over native per-provider document content blocks (e.g. Anthropic's
# {"type":"document",...}): extraction happens once, server-side, via the SAME
# tools/read_extract.py pipeline the read_file tool already uses (firecrawl-anydoc, a bundled
# core dependency — see hermes/pyproject.toml). The extracted text is inlined as a plain
# {"type":"text"} part, so it works identically across every provider/model with zero
# provider-adapter changes, and degrades the same way read_file already does: a missing/broken
# anydoc install or an unreadable document raises a clear, teaching ExtractionError message
# instead of a raw 500 (see read_extract._anydoc_missing_error). Trade-off: this is text-only
# extraction, not native vision-over-PDF-pages — a scanned/OCR-needed PDF surfaces
# read_extract's existing "[NEEDS OCR: ...]" note in the extracted text rather than silently
# working, which is the same behavior an agent calling read_file on that PDF already gets today.
#
# The uploaded bytes are never written to persistent storage: they are read into memory and
# handed to the extractor (which uses its own private tempfile internally, never the
# user-supplied filename, for path safety). The one exception is PDFs, which are ALWAYS converted
# to Markdown (tools/pdf_markdown.py) and the full conversion saved as a .md under
# get_hermes_home()/attachments/pdf-markdown/<32-hex>/<sanitized stem>.md, pruned after 30 days,
# so the agent can read_file past the inline text cap. There is no id-based lookup surface, so the
# path-traversal concerns that shape the music feature's track-id validation don't apply here —
# the client-supplied filename is only used, after being reduced to a bare basename, for
# extension sniffing and that sanitized .md stem.

_ATTACHMENT_FILENAME_MAX_LEN = 255
# Originally set to exactly match _cap_text()'s (api_server.py) 65,536-char ceiling on each
# normalized text content part, on the assumption that one attachment == one text part. That
# assumption broke in practice: a real document's front matter/table of contents alone routinely
# exceeds 64KB before reaching the content the user actually wanted (e.g. a large textbook's
# early chapters), so the whole attachment could be nothing but front matter. The dashboard now
# splits a long attachment across MULTIPLE <=64KB text parts instead of one oversized part (see
# dashboard.dc.html's CHUNK_CHARS), so this cap can be raised well past a single part's ceiling —
# the client-visible "truncated" flag still matches what actually reaches the model, just at a
# much more useful threshold.
_ATTACHMENT_EXTRACT_TEXT_CAP = 500_000


async def extract_chat_attachment(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    from gateway.platforms import api_server

    raw_name = request.headers.get("X-Attachment-Filename", "").strip()
    if not raw_name:
        return api_server._error_response(
            "X-Attachment-Filename header is required.", 400, code="missing_filename")
    try:
        filename = urllib.parse.unquote(raw_name)
    except Exception:
        return api_server._error_response(
            "Invalid X-Attachment-Filename encoding.", 400, code="invalid_filename")
    # Bare filename only — this is never used to touch the filesystem (see module note above),
    # but reducing it defensively means a client can never smuggle path separators into it.
    filename = os.path.basename(filename).strip()[:_ATTACHMENT_FILENAME_MAX_LEN] or "attachment"

    from . import pdf_markdown
    from tools.read_extract import is_extractable_document
    is_pdf = pdf_markdown.is_pdf(filename)
    # PDFs are accepted even when anydoc is unavailable: pdf_markdown falls back to pdftotext.
    if not (is_pdf or is_extractable_document(filename)):
        return api_server._error_response(
            "Unsupported document type for extraction. Supported: PDF, Word, PowerPoint, Excel "
            "(including legacy .doc/.ppt/.xls), OpenDocument, RTF, EPUB, Jupyter notebook.",
            415, code="unsupported_document_type")

    try:
        # Same bounded-accumulation pattern as upload_music (NOT request.content.read(n) —
        # see that function's comment for why).
        body = bytearray()
        async for chunk in request.content.iter_any():
            body.extend(chunk)
            if len(body) > CHAT_ATTACHMENT_EXTRACT_MAX_BYTES:
                break
        data = bytes(body)
    except Exception:
        return api_server._error_response("Failed to read request body.", 400, code="read_failed")
    if len(data) > CHAT_ATTACHMENT_EXTRACT_MAX_BYTES:
        return api_server._error_response(
            f"File exceeds the {CHAT_ATTACHMENT_EXTRACT_MAX_BYTES}-byte cap.",
            413, code="attachment_too_large")
    if not data:
        return api_server._error_response("Empty file body.", 400, code="empty_body")

    from tools.read_extract import ExtractionError, extract_document_bytes
    try:
        if is_pdf:
            text = await asyncio.to_thread(pdf_markdown.pdf_bytes_to_markdown, data, filename)
        else:
            text = await asyncio.to_thread(extract_document_bytes, data, filename)
    except ExtractionError as exc:
        return api_server._error_response(str(exc), 422, code="extraction_failed")
    except Exception as exc:  # noqa: BLE001 — never leak a raw 500 for user-supplied file content
        return api_server._error_response(f"Extraction failed: {exc}", 500, code="extraction_error")

    markdown_path = None
    if is_pdf:
        # The full, untruncated Markdown is kept on disk so the agent can read_file past the
        # inline cap below. A failed write still returns the conversion; the path is just null.
        with contextlib.suppress(OSError):
            markdown_path = str(await asyncio.to_thread(pdf_markdown.save_markdown, text, filename))

    truncated = len(text) > _ATTACHMENT_EXTRACT_TEXT_CAP
    if truncated:
        text = text[:_ATTACHMENT_EXTRACT_TEXT_CAP]
    payload = {"filename": filename, "text": text, "truncated": truncated}
    if is_pdf:
        payload.update(format="markdown", markdown_path=markdown_path)
    return web.json_response(payload)


# --- Notes (VESPER dashboard "NOTES") ----------------------------------------------------
#
# One JSON file per note at get_hermes_home()/vesper-notes/<32-hex id>.json, holding
# {"id", "title", "body", "created_at", "updated_at"} (timestamps are ISO-8601 UTC with
# millisecond precision and a "Z" suffix). There is no index or database: the list endpoint
# scans the directory. Because notes are plain JSON files under HERMES_HOME, the agent can read
# them with its ordinary file tools when the user asks about their notes -- no tool or API
# credential needed. The list skips anything that isn't a well-formed note (stray temp files, a
# hand-edited file that no longer parses) instead of failing, and GET/PATCH treat such a file
# as not found.
#
# Writes go temp file -> os.replace, so a reader (the list, the agent) never sees a half-written
# note. PATCH and DELETE also hold _NOTES_WRITE_LOCK across their whole read-modify-write: two
# concurrent PATCHes of different fields can't lose one another's edit, and a late PATCH can't
# resurrect a note a DELETE just removed. It is a threading lock taken inside the worker thread
# rather than an asyncio.Lock, so it stays valid across event loops and never stalls the loop.
#
# Client-supplied ids are checked against exactly 32 lowercase hex characters before any
# filesystem access (the same traversal guard as the music routes, but with fullmatch: "$"
# would accept a trailing newline). Reads never create the directory; the first write does,
# private to the owner.

_NOTE_ID_RE = re.compile(r"[0-9a-f]{32}")
_NOTE_TITLE_MAX_LEN = 200
_NOTE_BODY_MAX_LEN = 200_000
_NOTES_WRITE_LOCK = threading.Lock()


def _notes_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "vesper-notes"


def _notes_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _note_path(note_id):
    """None for anything that isn't a well-formed id, so callers never build a path from it."""
    if not _NOTE_ID_RE.fullmatch(note_id):
        return None
    return _notes_dir() / f"{note_id}.json"


def _read_note_file(path):
    """The note stored at *path*, or None if it is missing, unparseable or not note-shaped."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not all(isinstance(raw.get(key), str) for key in ("title", "body", "created_at", "updated_at")):
        return None
    # The filename is the identity; a stored "id" that disagrees with it is ignored.
    return {"id": path.stem, "title": raw["title"], "body": raw["body"],
            "created_at": raw["created_at"], "updated_at": raw["updated_at"]}


def _write_note_file(path, note):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")  # mode 0o600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # ensure_ascii=False keeps non-English notes readable to the agent's file tools.
            json.dump(note, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)  # only still there if the write failed before os.replace


def _scan_notes():
    directory = _notes_dir()
    if not directory.is_dir():
        return []
    notes = []
    for path in directory.iterdir():
        if path.suffix == ".json" and _NOTE_ID_RE.fullmatch(path.stem):
            note = _read_note_file(path)
            if note is not None:
                notes.append(note)
    notes.sort(key=lambda n: (n["updated_at"], n["created_at"], n["id"]), reverse=True)
    return notes


def _store_new_note(fields):
    now = _notes_timestamp()
    note = {"id": uuid.uuid4().hex, "title": "", "body": "", **fields,
            "created_at": now, "updated_at": now}
    _write_note_file(_notes_dir() / f"{note['id']}.json", note)
    return note


def _load_note(note_id):
    path = _note_path(note_id)
    return None if path is None else _read_note_file(path)


def _apply_note_patch(note_id, fields):
    path = _note_path(note_id)
    if path is None:
        return None
    with _NOTES_WRITE_LOCK:
        note = _read_note_file(path)
        if note is None:
            return None
        note.update(fields, updated_at=_notes_timestamp())
        _write_note_file(path, note)
    return note


def _remove_note(note_id):
    path = _note_path(note_id)
    if path is None:
        return False
    with _NOTES_WRITE_LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def _note_error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def _note_not_found():
    return _note_error(404, "note_not_found", "Note not found")


def _is_utf8_text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate escape ("\ud800") parses as JSON but can't be stored
        return False
    return True


async def _note_fields(request):
    """The ``title``/``body`` a create or patch body provides, as ``(fields, None)`` or
    ``(None, error_response)``. Titles are collapsed to one line; bodies are kept verbatim."""
    try:
        body = await request.json()
    except (ValueError, LookupError):  # bad JSON/UTF-8, or an unknown charset in Content-Type
        return None, _note_error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return None, _note_error(400, "invalid_note", "Expected a JSON object")
    fields = {}
    for name, limit in (("title", _NOTE_TITLE_MAX_LEN), ("body", _NOTE_BODY_MAX_LEN)):
        if name not in body:
            continue
        value = body[name]
        if not isinstance(value, str) or not _is_utf8_text(value):
            return None, _note_error(400, "invalid_note", f"{name} must be a string")
        if name == "title":
            value = " ".join(value.split())
        if len(value) > limit:
            return None, _note_error(
                400, f"note_{name}_too_long", f"{name} must be at most {limit} characters")
        fields[name] = value
    return fields, None


async def list_notes(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response({"notes": await asyncio.to_thread(_scan_notes)})


async def create_note(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    fields, error = await _note_fields(request)
    if error is not None:
        return error
    note = await asyncio.to_thread(_store_new_note, fields)
    return web.json_response({"note": note}, status=201)


async def get_note(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    note = await asyncio.to_thread(_load_note, request.match_info["note_id"])
    if note is None:
        return _note_not_found()
    return web.json_response({"note": note})


async def patch_note(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    fields, error = await _note_fields(request)
    if error is not None:
        return error
    note = await asyncio.to_thread(_apply_note_patch, request.match_info["note_id"], fields)
    if note is None:
        return _note_not_found()
    return web.json_response({"note": note})


async def delete_note(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    if not await asyncio.to_thread(_remove_note, request.match_info["note_id"]):
        return _note_not_found()
    return web.json_response({"deleted": True})


# --- Weather location (dashboard ATMOS panel) --------------------------------------------
#
# Browsers only expose navigator.geolocation on secure origins (https, localhost). A dashboard opened
# over plain http on the LAN (http://truenas.local:8642/vesper) therefore can never locate itself, and
# a phone, a PC and a tablet would each have to be located separately anyway. So one place is saved
# server-side for every device: get_hermes_home()/vesper-location.json = {"name", "lat", "lon"}. The
# user picks it by city name; /vesper/geocode proxies Open-Meteo's free, keyless geocoding API (the same
# provider the weather already comes from). /vesper/weather with no lat/lon uses the saved place.

_LOCATION_NAME_MAX_LEN = 120
_GEOCODE_QUERY_LEN = (2, 80)


def _location_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "vesper-location.json"


def _valid_location(raw):
    """``{"name", "lat", "lon"}`` if *raw* is a well-formed location, else None."""
    if not isinstance(raw, dict):
        return None
    name, lat, lon = raw.get("name"), raw.get("lat"), raw.get("lon")
    if not isinstance(name, str) or not name.strip() or len(name) > _LOCATION_NAME_MAX_LEN:
        return None
    if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"name": " ".join(name.split()), "lat": float(lat), "lon": float(lon)}


def load_saved_location():
    """The saved weather location, or None when unset or unreadable."""
    try:
        return _valid_location(json.loads(_location_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


async def get_location(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response({"location": await asyncio.to_thread(load_saved_location)})


async def put_location(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return _note_error(400, "invalid_json", "Invalid JSON body")
    location = _valid_location(body)
    if location is None:
        return _note_error(400, "invalid_location",
                           f"Expected {{name, lat, lon}}: a name up to {_LOCATION_NAME_MAX_LEN} characters, "
                           "lat between -90 and 90, lon between -180 and 180")
    await asyncio.to_thread(_write_note_file, _location_path(), location)
    return web.json_response({"location": location})


async def delete_location(adapter, request):
    error = adapter._check_auth(request)
    if error is not None:
        return error
    def _remove():
        with contextlib.suppress(FileNotFoundError):
            _location_path().unlink()
    await asyncio.to_thread(_remove)
    return web.json_response({"deleted": True})


async def geocode(adapter, request):
    """GET /vesper/geocode?q=<place> -- up to 6 matches from Open-Meteo's geocoding API."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    query = " ".join(request.query.get("q", "").split())
    lo, hi = _GEOCODE_QUERY_LEN
    if not lo <= len(query) <= hi:
        return _note_error(400, "invalid_query", f"Search for a place name of {lo}-{hi} characters")
    import aiohttp as _aiohttp_client
    params = {"name": query, "count": "6", "language": "en", "format": "json"}
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.get("https://geocoding-api.open-meteo.com/v1/search", params=params,
                                   timeout=_aiohttp_client.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return _note_error(502, "geocode_failed", f"Place search failed (HTTP {resp.status})")
                data = await resp.json()
    except Exception as exc:
        return _note_error(502, "geocode_failed", f"Place search failed: {exc}")
    results = []
    for item in (data or {}).get("results") or []:
        parts = [item.get("name"), item.get("admin1"), item.get("country")]
        label = ", ".join(dict.fromkeys(p for p in parts if p))  # dedupe "Singapore, Singapore"
        location = _valid_location({"name": label, "lat": item.get("latitude"), "lon": item.get("longitude")})
        if location:
            results.append(location)
    return web.json_response({"results": results})
