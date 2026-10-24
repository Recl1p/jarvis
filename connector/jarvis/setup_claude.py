"""Claude Pro/Max sign-in for the dashboard setup guide, done by Anthropic's own Claude Code CLI.

The guide never handles Claude OAuth itself: it runs ``claude setup-token`` in a pseudo-terminal, shows the user the
sign-in link Claude Code prints, and types the code the user pastes back into Claude Code's prompt. Claude Code is
not in the image: the first time someone picks Claude, ``/vesper/setup/claude/install`` downloads the pinned native
build from npm, checks it against npm's SHA-512 and keeps it under HERMES_HOME (so it survives image rebuilds). Claude Code prints a long-lived token for use with your plan (Anthropic
allows third-party apps on plan limits: support.claude.com/en/articles/15036540); it is saved the way
``hermes auth add anthropic`` saves a setup-token, and Claude becomes the main model.

Each sign-in runs with a throwaway HOME that is deleted afterwards. The token is never logged or returned.
"""

import asyncio
import base64
import fcntl
import hashlib
import logging
import os
import platform
import pty
import re
import secrets
import shutil
import signal
import struct
import subprocess
import tarfile
import tempfile
import termios
import threading
import time
import urllib.request
from functools import partial
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Pinned Claude Code build per CPU: (npm package suffix, SRI from `npm view ... dist.integrity`, download size).
CLAUDE_CODE_VERSION = "2.1.284"
CLAUDE_CODE_BUILDS = {
    "x86_64": ("linux-x64", "sha512-hjjPgN4u8DvnzZqWDYnU5xxQkykuUrkUoVeeutVLXjrhx4qeRPmwgZ69BFAHmV42FBRdiRwbLbiDE9nkQdAE8g==",
               108_750_001),
    "aarch64": ("linux-arm64", "sha512-S2bS/X/WNr4oe5c2qOdWGIfL9O/oTVLG7mlCI8FNLPWTQ2tNoeaLFPUzpyEkB4aIv4CL8+dw6YocT0JUDoI/1A==",
                0),
}
CLAUDE_CODE_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code-{suffix}/-/claude-code-{suffix}-{version}.tgz"
START_TIMEOUT = 45      # seconds for Claude Code to print its sign-in link
SUBMIT_TIMEOUT = 60     # seconds for it to trade the pasted code for a token
SESSION_TTL = 15 * 60
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\r")
_URL = re.compile(r"https://(?:claude\.com|claude\.ai)/\S*oauth/authorize\?\S+")
_ANSWER = re.compile(r"(?P<token>sk-ant-oat\d{2}-[A-Za-z0-9_-]{20,})"
                     r"|(?i:\b(?:invalid|error|failed|expired|denied|refused|not permit|on hold|unauthori[sz]ed)\b)")
# Claude Code (Ink) lays words out with cursor moves instead of spaces; turn those back into spaces.
_CURSOR_GAP = re.compile(r"\x1b\[\d*[GC]")
_SECRETISH = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_CODE_SHAPE = re.compile(r"[\x21-\x7e]{8,600}")

_sessions = {}
_lock = threading.Lock()
_install = {"state": "idle", "done": 0, "total": 0, "error": ""}   # idle | downloading | error


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def http_routes(adapter):
    return [
        ("GET", "/vesper/setup/claude/status", partial(claude_status, adapter)),
        ("POST", "/vesper/setup/claude/install", partial(claude_install, adapter)),
        ("POST", "/vesper/setup/claude/start", partial(claude_start, adapter)),
        ("POST", "/vesper/setup/claude/use", partial(claude_use, adapter)),
        ("POST", "/vesper/setup/claude/submit", partial(claude_submit, adapter)),
        ("DELETE", "/vesper/setup/claude/{session_id}", partial(claude_cancel, adapter)),
    ]


def plan_connected():
    """Is a Claude plan already signed in (a setup-token from this guide or the terminal, or Hermes' own Claude
    login)? An Anthropic API key doesn't count: that's the API key tab's connection."""
    from agent.anthropic_credentials import read_hermes_oauth_credentials
    from hermes_cli.config import get_env_value_prefer_dotenv
    if any((get_env_value_prefer_dotenv(var) or "").strip() for var in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")):
        return True
    try:
        return bool((read_hermes_oauth_credentials() or {}).get("accessToken"))
    except Exception:
        return False


def _use_plan():
    from .setup_providers import _confirm_saved, _default_model
    from hermes_cli.model_setup_flows_common import _persist_model
    model = _default_model("anthropic")
    if not model:
        return None, "No Claude model is listed"
    _persist_model(model, "anthropic", drop_base_url=True, drop_api_mode=True)
    return model, _confirm_saved("anthropic", model)


def _install_root():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "claude-code"


def claude_code_binary():
    """Path of the Claude Code CLI the guide downloaded (or one on PATH), or "" when there is none yet."""
    path = _install_root() / CLAUDE_CODE_VERSION / "claude"
    return str(path) if os.access(path, os.X_OK) else (shutil.which("claude") or "")


def _install_status():
    with _lock:
        status = dict(_install)
    return {**status, "installed": bool(claude_code_binary()), "version": CLAUDE_CODE_VERSION}


def _download():
    """Fetch the pinned build, verify it, unpack the one binary, then drop older versions. Runs on its own thread."""
    build = CLAUDE_CODE_BUILDS.get(platform.machine())
    target = _install_root() / CLAUDE_CODE_VERSION
    archive, part = target / ".download.tgz", target / ".claude.part"
    try:
        if build is None:
            raise RuntimeError(f"Claude Code isn't available for this computer's processor ({platform.machine()})")
        suffix, sri, size = build
        target.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha512()
        url = CLAUDE_CODE_URL.format(suffix=suffix, version=CLAUDE_CODE_VERSION)
        with urllib.request.urlopen(url, timeout=30) as resp, open(archive, "wb") as out:
            with _lock:
                _install["total"] = int(resp.headers.get("Content-Length") or 0) or size
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
                with _lock:
                    _install["done"] += len(chunk)
        if "sha512-" + base64.b64encode(digest.digest()).decode() != sri:
            raise RuntimeError("The Claude Code download didn't match its published checksum, so it wasn't installed")
        with tarfile.open(archive) as tar, open(part, "wb") as out:
            shutil.copyfileobj(tar.extractfile("package/claude"), out)
        part.chmod(0o755)
        os.replace(part, target / "claude")
        for old in _install_root().iterdir():
            if old.is_dir() and old.name != CLAUDE_CODE_VERSION:
                shutil.rmtree(old, ignore_errors=True)
        with _lock:
            _install.update(state="idle", error="")
    except Exception as exc:
        logger.warning("Claude Code download failed: %s", type(exc).__name__)
        message = str(exc) if isinstance(exc, RuntimeError) else "Claude Code couldn't be downloaded. Check the internet connection and try again."
        with _lock:
            _install.update(state="error", error=message)
    finally:
        for leftover in (archive, part):
            leftover.unlink(missing_ok=True)


def _begin_install():
    """Start the download unless Claude Code is already here or on its way. Returns the status to show."""
    if not claude_code_binary():
        with _lock:
            starting = _install["state"] != "downloading"
            if starting:
                _install.update(state="downloading", done=0, total=0, error="")
        if starting:
            threading.Thread(target=_download, daemon=True, name="vesper-claude-download").start()
    return _install_status()


class _Login:
    """One ``claude setup-token`` run: a child on a pty whose output a reader thread collects."""

    def __init__(self, binary):
        self.home = tempfile.mkdtemp(prefix="vesper-claude-")
        master, slave = pty.openpty()
        # A wide terminal keeps the sign-in link and the token on one line each.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
        env = {"HOME": self.home, "PATH": "/usr/local/bin:/usr/bin:/bin", "TERM": "xterm-256color",
               "LANG": "C.UTF-8", "BROWSER": "/bin/false", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        self.proc = subprocess.Popen([binary, "setup-token"], stdin=slave, stdout=slave, stderr=slave, env=env,
                                     cwd=self.home, start_new_session=True, close_fds=True)
        os.close(slave)
        self.master, self.created, self.raw = master, time.monotonic(), b""
        self.changed = threading.Condition()
        threading.Thread(target=self._read, daemon=True, name="vesper-claude-login").start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                chunk = b""
            with self.changed:
                if not chunk:
                    self.raw += b"\0"   # marks the end of output
                    self.changed.notify_all()
                    return
                self.raw += chunk
                self.changed.notify_all()

    def text(self):
        return _ANSI.sub("", _CURSOR_GAP.sub(" ", self.raw.decode("utf-8", "replace")))

    def last_words(self, since, limit=300):
        """What Claude Code showed after offset *since* (minus the echo of what was typed), on one line, secrets masked."""
        after = self.text()[since:].replace("\0", "")
        said = " ".join(after.split("\n", 1)[-1].split())
        return _SECRETISH.sub("[hidden]", said)[-limit:]

    def wait_for(self, pattern, timeout, since=0):
        """The first *pattern* match in the output after offset *since*, or None at timeout or exit."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                found = pattern.search(self.text(), since)
                if found or self.raw.endswith(b"\0"):
                    return found
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.changed.wait(left)

    def type(self, text):
        """Type *text*, then press Enter on its own. Claude Code reads a long burst of input as a paste, and an
        Enter inside a paste is just more text: a real sign-in code (100+ characters) sent with its Enter in one
        write was never submitted."""
        os.write(self.master, text.encode())
        time.sleep(0.6)
        os.write(self.master, b"\r")

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        shutil.rmtree(self.home, ignore_errors=True)


def _gc():
    with _lock:
        stale = [sid for sid, s in _sessions.items() if time.monotonic() - s.created > SESSION_TTL]
        for sid in stale:
            _sessions.pop(sid).close()


def _start_login(binary):
    login = _Login(binary)
    found = login.wait_for(_URL, START_TIMEOUT)
    if not found:
        login.close()
        return None, None
    sid = secrets.token_urlsafe(16)
    with _lock:
        _sessions[sid] = login
    return sid, found.group(0)


def _finish_login(login, code):
    """Type *code* into Claude Code; returns (token, None) or (None, problem)."""
    since = len(login.text())
    login.type(code)
    # Whichever comes first: the token, or Claude Code saying the code failed. Claude Code echoes the typed code
    # masked on its own line, which can't match either.
    answer = login.wait_for(_ANSWER, SUBMIT_TIMEOUT, since)
    if answer and answer.group("token"):
        return answer.group("token"), None
    said = login.last_words(since)
    logger.warning("claude setup-token did not finish: %s", said or "(no output)")
    if answer:
        return None, f"Claude didn't accept that code. Claude Code said: “{said}”. Start the sign-in again."
    return None, (f"Claude Code stopped at a step the guide doesn't handle yet. It said: “{said}”"
                  if said else "Claude didn't answer in time. Start the sign-in again.")


def _save_token_and_model(token):
    from .setup_providers import _confirm_saved, _default_model
    from hermes_cli.config import get_env_value_prefer_dotenv, save_anthropic_oauth_token, save_env_value
    from hermes_cli.model_setup_flows_common import _persist_model
    save_anthropic_oauth_token(token, save_fn=save_env_value)
    if get_env_value_prefer_dotenv("ANTHROPIC_TOKEN") != token:
        return None, "Hermes could not save the Claude sign-in (the configuration is locked)"
    model = _default_model("anthropic")
    if not model:
        return None, "Signed in, but no Claude model is listed"
    _persist_model(model, "anthropic", drop_base_url=True, drop_api_mode=True)
    return model, _confirm_saved("anthropic", model)


async def claude_status(adapter, request):
    """GET /vesper/setup/claude/status -- is Claude Code downloaded, and how far along is a download?"""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response(await asyncio.to_thread(_install_status))


async def claude_install(adapter, request):
    """POST /vesper/setup/claude/install -- download Claude Code (first Claude sign-in only); poll status after."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    return web.json_response(await asyncio.to_thread(_begin_install))


async def claude_start(adapter, request):
    """POST /vesper/setup/claude/start -- run ``claude setup-token``; returns the sign-in link it prints."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    binary = claude_code_binary()
    if not binary:
        return _error(409, "not_installed", "Claude Code isn't downloaded yet")
    _gc()
    try:
        sid, url = await asyncio.to_thread(_start_login, binary)
    except OSError as exc:
        logger.warning("claude setup-token could not start: %s", type(exc).__name__)
        sid, url = None, None
    if not sid:
        return _error(502, "login_unavailable", "Claude's sign-in didn't start. Try again in a minute.")
    return web.json_response({"session_id": sid, "url": url})


async def claude_submit(adapter, request):
    """POST /vesper/setup/claude/submit {session_id, code} -- give Claude Code the code from the sign-in page."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return _error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return _error(400, "invalid_request", "Expected a JSON object")
    sid = body.get("session_id") if isinstance(body.get("session_id"), str) else ""
    code = body.get("code").strip() if isinstance(body.get("code"), str) else ""
    if not _CODE_SHAPE.fullmatch(code):
        return _error(400, "invalid_code", "Paste the code from Claude's page (one piece of text, no spaces)")
    with _lock:
        login = _sessions.pop(sid, None)
    if login is None:
        return _error(404, "login_not_found", "That sign-in expired. Start it again.")
    try:
        token, problem = await asyncio.to_thread(_finish_login, login, code)
    finally:
        await asyncio.to_thread(login.close)
    if problem:
        return _error(400, "login_failed", problem)
    try:
        model, problem = await asyncio.to_thread(_save_token_and_model, token)
    except Exception as exc:
        logger.warning("claude sign-in could not be saved: %s", type(exc).__name__)
        model, problem = None, "Hermes could not save the Claude sign-in"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": "anthropic", "model": model})


async def claude_use(adapter, request):
    """POST /vesper/setup/claude/use -- already signed in: make Claude the main model again, no new sign-in."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    if not await asyncio.to_thread(plan_connected):
        return _error(409, "not_connected", "No Claude plan is signed in yet")
    try:
        model, problem = await asyncio.to_thread(_use_plan)
    except Exception as exc:
        logger.warning("switching to the Claude plan failed: %s", type(exc).__name__)
        model, problem = None, "Hermes could not switch to Claude"
    if problem:
        return _error(500, "save_failed", problem)
    return web.json_response({"ok": True, "provider": "anthropic", "model": model})


async def claude_cancel(adapter, request):
    """DELETE /vesper/setup/claude/{session_id} -- stop a sign-in that wasn't finished."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    with _lock:
        login = _sessions.pop(request.match_info["session_id"], None)
    if login is not None:
        await asyncio.to_thread(login.close)
    return web.json_response({"ok": login is not None})
