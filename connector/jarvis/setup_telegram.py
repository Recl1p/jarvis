"""Telegram for the dashboard guide (its Telegram step), with a bot the user makes in @BotFather. No terminal needed.

1. ``POST /vesper/setup/telegram/check {token}``: Telegram's getMe names the bot. The token stays in this process
   under a setup id and is never sent back.
2. The user opens the bot and presses Start. ``POST /vesper/setup/telegram/find {setup_id}``: getUpdates on the new
   token lists who wrote to it (private chats only), so the user picks themselves instead of looking up an id.
3. ``POST /vesper/setup/telegram/connect {setup_id, user_id}`` saves what ``hermes gateway setup`` saves
   (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, TELEGRAM_HOME_CHANNEL), turns the platform on, marks the setup
   messages read so the gateway doesn't answer them, says hello in the chat, and starts the Telegram adapter in the
   running gateway through its reconnect queue, so no restart is needed.

``GET /vesper/setup/telegram`` says whether Telegram is set up and running, and as which bot.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import time
from functools import partial

from aiohttp import web

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
SETUP_TTL = 30 * 60
CALL_TIMEOUT = 15
BOT_CACHE_TTL = 600
# Same rule as Hermes' own setup (hermes_cli/setup_platforms.py).
_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_USER_ID = re.compile(r"\d{1,20}")
HELLO = "✓ {name} is connected. Message me here any time."
# persona id -> the name the hello is signed with
NAMES = {"jarvis": "JARVIS", "cortana": "Cortana", "ultron": "Ultron"}

_pending = {}      # setup id -> {token, bot, senders, last_update, created}
_bot_cache = {}    # sha256(token) -> (time, bot)


def _error(status, code, message):
    from gateway.platforms import api_server
    return api_server._error_response(message, status, code=code)


def http_routes(adapter):
    return [
        ("GET", "/vesper/setup/telegram", partial(telegram_status, adapter)),
        ("POST", "/vesper/setup/telegram/check", partial(check_token, adapter)),
        ("POST", "/vesper/setup/telegram/find", partial(find_senders, adapter)),
        ("POST", "/vesper/setup/telegram/connect", partial(connect, adapter)),
    ]


async def _json_body(request):
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return None, _error(400, "invalid_json", "Invalid JSON body")
    if not isinstance(body, dict):
        return None, _error(400, "invalid_request", "Expected a JSON object")
    return body, None


class TelegramError(Exception):
    def __init__(self, status, description=""):
        super().__init__(status)
        self.status, self.description = status, description


async def _call(token, method, **params):
    """One Bot API call -> its result. Raises TelegramError (status 0 when Telegram wasn't reachable)."""
    import aiohttp as _aiohttp_client
    try:
        async with _aiohttp_client.ClientSession() as session:
            async with session.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params,
                                    timeout=_aiohttp_client.ClientTimeout(total=CALL_TIMEOUT)) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
    except Exception as exc:
        logger.warning("Telegram %s failed: %s", method, type(exc).__name__)
        raise TelegramError(0) from None
    if isinstance(data, dict) and data.get("ok"):
        return data.get("result")
    description = data.get("description") if isinstance(data, dict) else ""
    raise TelegramError(resp.status, description if isinstance(description, str) else "")


def _bot_row(me):
    username = str(me.get("username") or "")
    return {"username": username, "name": str(me.get("first_name") or username),
            "link": f"https://t.me/{username}" if username else ""}


def _sweep():
    now = time.monotonic()
    for sid in [sid for sid, p in _pending.items() if now - p["created"] > SETUP_TTL]:
        _pending.pop(sid, None)


def _pending_or_error(body):
    _sweep()
    pending = _pending.get(body.get("setup_id")) if isinstance(body.get("setup_id"), str) else None
    if pending is None:
        return None, _error(404, "setup_expired", "That setup expired. Paste the bot token again.")
    return pending, None


def _problem(exc, doing):
    if exc.status == 0:
        return _error(502, "unreachable", "Telegram isn't reachable right now. Check the internet connection and try again.")
    if exc.status == 401:
        return _error(400, "invalid_token", "Telegram didn't accept that token. Copy it again from @BotFather.")
    if exc.status == 409:
        return _error(409, "bot_in_use", "This bot is already connected to another program, so it can't be set up "
                                         "here. Make a new bot in @BotFather, or stop the other program first.")
    return _error(502, "telegram_error", f"Telegram couldn't {doing} ({exc.description or exc.status}).")


# ---- Status -----------------------------------------------------------------------------------

def _saved(name):
    from hermes_cli.config import get_env_value_prefer_dotenv
    return (get_env_value_prefer_dotenv(name) or "").strip()


def _runner(adapter):
    runner = getattr(adapter, "gateway_runner", None)
    return runner if runner is not None and getattr(runner, "_running", False) else None


def _platform_state():
    from gateway.status import read_runtime_status
    state = ((read_runtime_status() or {}).get("platforms") or {}).get("telegram") or {}
    return state.get("state") or "", state.get("error_message") or ""


async def _bot_for(token):
    """The saved bot's name, cached; None when Telegram can't say."""
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _bot_cache.get(key)
    if hit and time.monotonic() - hit[0] < BOT_CACHE_TTL:
        return hit[1]
    try:
        bot = _bot_row(await _call(token, "getMe"))
    except TelegramError:
        return None
    _bot_cache[key] = (time.monotonic(), bot)
    return bot


async def telegram_status(adapter, request):
    """GET /vesper/setup/telegram -- {configured, running, state, error, bot, allowed}."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    token = await asyncio.to_thread(_saved, "TELEGRAM_BOT_TOKEN")
    allowed = [u for u in (await asyncio.to_thread(_saved, "TELEGRAM_ALLOWED_USERS")).split(",") if u.strip()]
    state, message = await asyncio.to_thread(_platform_state)
    runner = _runner(adapter)
    from gateway.config import Platform
    running = bool(token) and (Platform.TELEGRAM in runner.adapters if runner is not None else state == "connected")
    return web.json_response({
        "configured": bool(token), "running": running, "state": state if token else "",
        "error": message if token and not running else "", "allowed": len(allowed),
        "bot": await _bot_for(token) if token else None})


# ---- Setup ------------------------------------------------------------------------------------

async def check_token(adapter, request):
    """POST /vesper/setup/telegram/check {token} -- is this a working bot token? -> {setup_id, bot}."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    token = body.get("token").strip() if isinstance(body.get("token"), str) else ""
    if not token:
        return _error(400, "missing_token", "Paste the bot token from @BotFather to continue")
    if not _TOKEN.fullmatch(token):
        return _error(400, "invalid_token", "That isn't a bot token. It looks like 123456789:AAE… and comes from @BotFather.")
    try:
        bot = _bot_row(await _call(token, "getMe"))
    except TelegramError as exc:
        return _problem(exc, "check the bot")
    _sweep()
    setup_id = secrets.token_urlsafe(18)
    _pending[setup_id] = {"token": token, "bot": bot, "senders": {}, "last_update": None, "created": time.monotonic()}
    same = token == await asyncio.to_thread(_saved, "TELEGRAM_BOT_TOKEN")
    return web.json_response({"setup_id": setup_id, "bot": bot, "already_connected": same})


def _sender(update):
    msg = update.get("message") if isinstance(update, dict) else None
    if not isinstance(msg, dict) or (msg.get("chat") or {}).get("type") != "private":
        return None
    who = msg.get("from") or {}
    if who.get("is_bot") or not _USER_ID.fullmatch(str(who.get("id") or "")):
        return None
    name = " ".join(str(who.get(k) or "") for k in ("first_name", "last_name")).strip()
    return {"id": str(who["id"]), "name": name[:80] or "Telegram user", "username": str(who.get("username") or "")[:64]}


async def find_senders(adapter, request):
    """POST /vesper/setup/telegram/find {setup_id} -- who has written to the new bot -> {senders: [...]}."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    pending, error = _pending_or_error(body)
    if error is not None:
        return error
    runner = _runner(adapter)
    from gateway.config import Platform
    if runner is not None and Platform.TELEGRAM in runner.adapters and pending["token"] == await asyncio.to_thread(_saved, "TELEGRAM_BOT_TOKEN"):
        # The gateway is reading this bot's messages itself; a second reader would knock it off.
        return _error(409, "already_connected", "This bot is already connected to JARVIS.")
    try:
        updates = await _call(pending["token"], "getUpdates", timeout=0, allowed_updates=["message"])
    except TelegramError as exc:
        return _problem(exc, "read the bot's messages")
    for update in updates if isinstance(updates, list) else []:
        if isinstance(update, dict) and isinstance(update.get("update_id"), int):
            pending["last_update"] = max(pending["last_update"] or 0, update["update_id"])
        who = _sender(update)
        if who:
            pending["senders"][who["id"]] = who
    return web.json_response({"senders": list(pending["senders"].values())})


def _save(token, user_id):
    """Save the bot the way `hermes gateway setup` does. A new bot starts with just this user; the same bot gains one."""
    from hermes_cli.config import save_env_value, write_platform_config_field
    same_bot = _saved("TELEGRAM_BOT_TOKEN") == token
    allowed = [u.strip() for u in _saved("TELEGRAM_ALLOWED_USERS").split(",") if u.strip()] if same_bot else []
    save_env_value("TELEGRAM_BOT_TOKEN", token)
    save_env_value("TELEGRAM_ALLOWED_USERS", ",".join(dict.fromkeys([*allowed, user_id])))
    if not (same_bot and _saved("TELEGRAM_HOME_CHANNEL")):
        save_env_value("TELEGRAM_HOME_CHANNEL", user_id)
    write_platform_config_field("telegram", "enabled", True)
    if _saved("TELEGRAM_BOT_TOKEN") != token:
        return "Hermes could not save the bot token (the configuration is locked)"
    return None


async def _start_in_gateway(adapter):
    """Hand the saved bot to the running gateway's reconnect queue (gateway/run_adapters.py), replacing a Telegram
    adapter that's already running. False when there's no gateway in this process to start it in."""
    runner = _runner(adapter)
    if runner is None:
        return False
    from gateway.config import Platform, load_gateway_config
    pconf = (await asyncio.to_thread(load_gateway_config)).platforms.get(Platform.TELEGRAM)
    if pconf is None or not pconf.enabled or not pconf.token:
        return False
    old = runner.adapters.pop(Platform.TELEGRAM, None)
    if old is not None:
        await runner._bounded_adapter_teardown(old, Platform.TELEGRAM)
    runner.config.platforms[Platform.TELEGRAM] = pconf
    runner._failed_platforms[Platform.TELEGRAM] = runner._reconnect_queue_entry(
        Platform.TELEGRAM, None, pconf, attempts=0, delay=0.0)
    runner._ensure_reconnect_watcher_running()
    return True


async def connect(adapter, request):
    """POST /vesper/setup/telegram/connect {setup_id, user_id, persona?} -- save the bot for this user and start it."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    body, error = await _json_body(request)
    if error is not None:
        return error
    pending, error = _pending_or_error(body)
    if error is not None:
        return error
    user_id = str(body.get("user_id") or "")
    if user_id not in pending["senders"]:
        return _error(400, "unknown_user", "Pick the account that messaged the bot")
    try:
        problem = await asyncio.to_thread(_save, pending["token"], user_id)
    except Exception as exc:
        logger.warning("Telegram setup could not be saved: %s", type(exc).__name__)
        problem = "Hermes could not save the Telegram setup"
    if problem:
        return _error(500, "save_failed", problem)
    token = pending["token"]
    _pending.pop(body["setup_id"], None)
    try:
        if pending["last_update"] is not None:   # read: the gateway doesn't answer the setup messages
            await _call(token, "getUpdates", offset=pending["last_update"] + 1, timeout=0)
        await _call(token, "sendMessage", chat_id=int(user_id),
                    text=HELLO.format(name=NAMES.get(body.get("persona"), "JARVIS")))
    except TelegramError:
        pass   # the setup is saved either way; the hello is a courtesy
    try:
        started = await _start_in_gateway(adapter)
    except Exception as exc:
        logger.warning("Telegram adapter could not be started: %s", type(exc).__name__)
        started = False
    return web.json_response({"ok": True, "bot": pending["bot"], "started": started,
                              "restart_needed": not started})
