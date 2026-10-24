"""The dashboard's voice (POST /vesper/tts): five engines the user picks between in the dashboard (VOICE, under the
persona), each falling back to a quicker one when it fails.

    elevenlabs   ElevenLabs, online: each persona's voice from the user's own ElevenLabs account (personas.js
                 voice.elevenlabs). Uses the account's characters; when they run out, Pocket TTS speaks.
    pocket       Pocket TTS (Kyutai), on the NAS: about three times faster than real time on its CPU (HANDOFF 32).
                 Each persona has one of Pocket TTS's own catalog voices (tts.pocket.voices). With Kyutai's gated
                 voice-cloning weights at $HERMES_HOME/pocket-tts/model.safetensors it speaks in the persona's
                 reference clip instead, as OmniVoice does.
    omnivoice    OmniVoice (k2-fsa), on the NAS: clones the persona's reference clip, but several times slower than
                 real time, so a sentence takes 10-50 s the first time.
    edge         Microsoft Edge's online voices: free, no account.
    piper        Piper, on the NAS: instant and plain. Every engine's last resort.

The two NAS models run in the image's voice interpreter (/opt/omnivoice/venv, see Dockerfile.hermes) as worker
processes (vesper_pocket_worker.py, vesper_omnivoice_worker.py): started on first use or when the dashboard opens,
stopped when idle so their memory goes back to the NAS. Requests to one worker are serialized; one whose browser has
already gone away (the user talked over the voice) is skipped before it starts.

The reference clip of a persona is $HERMES_HOME/vesper-voices/<persona>.wav + .txt when the user put one there, else
the shipped assets/vesper/voices/<persona>.wav (original voices made with OmniVoice's voice design).

Everything synthesized is cached on disk by (engine, voice, settings, text), which also saves ElevenLabs characters:
the dashboard's fixed lines ("On it", greetings) are instant after the first time, and POST /vesper/voice/prewarm
renders a list of them for OmniVoice while it's idle.

Settings (config.yaml):
    tts.omnivoice   steps (4), guidance (0.0), int8 (false), threads (3), idle_minutes (15), enabled (true)
    tts.pocket      threads (3), idle_minutes (30), enabled (true), voices ({persona: catalog voice})
    tts.elevenlabs  model_id etc., as for Hermes' own voice replies
"""

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from functools import partial
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

OMNIVOICE_ROOT = Path("/opt/omnivoice")
OMNIVOICE_PYTHON = OMNIVOICE_ROOT / "venv" / "bin" / "python"   # the image's voice interpreter: runs Pocket TTS
# OmniVoice installed from the setup screen (launcher/jarvis_launcher/voices.py): $HERMES_HOME/omnivoice, complete once
# its marker file is written.
OMNIVOICE_DATA_DIR = "omnivoice"
OMNIVOICE_MARKER = ".installed"
OMNIVOICE_DEVICES = ("auto", "cpu", "cuda")
WORKER = Path(__file__).resolve().parent / "omnivoice_worker.py"
POCKET_WORKER = Path(__file__).resolve().parent / "pocket_worker.py"
POCKET_HF = Path("/opt/pocket-tts/hf")
SHIPPED_VOICES = Path(__file__).resolve().parent / "assets" / "vesper" / "voices"
PERSONAS = ("jarvis", "ultron", "cortana")

ENGINES = {
    "elevenlabs": {"label": "ElevenLabs", "note": "Online · your ElevenLabs voices", "local": False},
    "pocket": {"label": "Pocket TTS", "note": "On the NAS · fast", "local": True},
    "omnivoice": {"label": "OmniVoice", "note": "On the NAS · cloned voices · slow", "local": True},
    "edge": {"label": "Microsoft Edge", "note": "Online · free", "local": False},
    "piper": {"label": "Piper", "note": "On the NAS · instant · plain", "local": True},
}
# When an engine fails (no characters left, offline, a worker that won't start), the next one in its list speaks;
# Piper is always last.
FALLBACKS = {
    "elevenlabs": ("pocket",),
    "pocket": (),
    "omnivoice": ("pocket",),
    "edge": ("pocket",),
    "piper": (),
}
# Without a choice saved in the dashboard: the user's ElevenLabs voices when there's a key, else the fast local one.
DEFAULT_ORDER = ("elevenlabs", "pocket", "omnivoice", "edge", "piper")

# Picked on the NAS (HANDOFF 31) by Whisper word errors over the same sentences: fp32 without guidance at 4 steps
# (RTF ~5, 4% WER) is about twice as quick as 8 steps (RTF ~12, 0%) for nearly the same clarity; int8 was faster
# still but garbled words, so it stays off. config.yaml tts.omnivoice.steps: 8 trades speed for quality.
DEFAULTS = {"steps": 4, "guidance": 0.0, "int8": False, "threads": 3, "idle_minutes": 15, "enabled": True}
# tts.omnivoice.device: auto (an NVIDIA GPU when the container has one, else the CPU), cpu or cuda.
# Pocket TTS catalog voices (CC0 / CC-BY, kyutai/tts-voices), picked on the NAS (HANDOFF 32) for each persona's
# register: a British narrator for JARVIS, the lowest male voice for Ultron, an American woman for Cortana.
POCKET_DEFAULTS = {"threads": 3, "idle_minutes": 30, "enabled": True,
                   "voices": {"jarvis": "peter_yearsley", "ultron": "javert", "cortana": "eve"}}
POCKET_CATALOG = ("alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette", "eponine", "eve",
                  "fantine", "george", "jane", "javert", "jean", "marius", "mary", "michael", "paul",
                  "peter_yearsley", "stuart_bell", "vera")
CACHE_LIMIT_BYTES = 300 * 1024 * 1024
CACHE_VERSION = 3   # bump when the worker's output changes (3: gentler trimming, short lines at full quality)
# Short lines ("On it.", "Hi there.") come out as mumbles at the fast settings; a sentence of five words or more
# is clear at 4 steps (HANDOFF 31: Whisper heard nothing in 1-3 word lines at 4-8 steps, every word at 16 + guidance).
SHORT_WORDS = 4
CAREFUL = {"steps": 16, "guidance": 2.0}


def quality_for(text: str, cfg: dict, background: bool = False) -> dict:
    """OmniVoice steps and guidance for one line: the fast defaults, or CAREFUL for short lines and prewarmed ones."""
    if background or len(text.split()) <= SHORT_WORDS:
        return {"steps": max(cfg["steps"], CAREFUL["steps"]), "guidance": max(cfg["guidance"], CAREFUL["guidance"])}
    return {"steps": cfg["steps"], "guidance": cfg["guidance"]}


def http_routes(adapter):
    return [
        ("GET", "/vesper/voice/status", partial(voice_status, adapter)),
        ("POST", "/vesper/voice/prewarm", partial(voice_prewarm, adapter)),
    ]


def _tts_config() -> dict:
    from tools.tts_tool import _load_tts_config
    return _load_tts_config() or {}


def _merge(defaults: dict, raw) -> dict:
    """*defaults* with the config's values of the same type (anything else in config.yaml is ignored)."""
    out = dict(defaults)
    raw = raw if isinstance(raw, dict) else {}
    for key, default in defaults.items():
        value = raw.get(key)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
        elif isinstance(default, (int, float)) and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = type(default)(value)
    out["threads"] = max(1, min(os.cpu_count() or 1, out["threads"]))
    return out


def settings() -> dict:
    raw = _tts_config().get("omnivoice")
    out = _merge(DEFAULTS, raw)
    out["steps"] = max(2, min(64, out["steps"]))
    device = raw.get("device") if isinstance(raw, dict) else None
    out["device"] = device if device in OMNIVOICE_DEVICES else "auto"
    return out


def pocket_settings() -> dict:
    raw = _tts_config().get("pocket")
    out = _merge(POCKET_DEFAULTS, raw)
    chosen = raw.get("voices") if isinstance(raw, dict) else None
    out["voices"] = {p: (chosen.get(p) if isinstance(chosen, dict) and chosen.get(p) in POCKET_CATALOG else v)
                     for p, v in POCKET_DEFAULTS["voices"].items()}
    return out


def _snapshot(root: Path):
    snaps = sorted((root / "hf" / "hub" / "models--k2-fsa--OmniVoice" / "snapshots").glob("*/config.json"))
    return snaps[0].parent if snaps else None


def omnivoice_root():
    """Where OmniVoice is: built into the image (a build with JARVIS_LOCAL_VOICES=1), else installed into the data
    volume from the setup screen (a CPU or an NVIDIA GPU build), else None."""
    if _snapshot(OMNIVOICE_ROOT) is not None:
        return OMNIVOICE_ROOT
    from hermes_constants import get_hermes_home
    root = Path(get_hermes_home()) / OMNIVOICE_DATA_DIR
    return root if (root / OMNIVOICE_MARKER).is_file() else None


def omnivoice_python():
    root = omnivoice_root()
    return root / "venv" / "bin" / "python" if root is not None else None


def model_dir():
    """OmniVoice's model snapshot (a local folder, so the worker never needs the network or a hub 'main' ref)."""
    root = omnivoice_root()
    return _snapshot(root) if root is not None else None


def installed() -> bool:
    """OmniVoice is in the image or was installed from the setup screen."""
    python = omnivoice_python()
    return python is not None and python.is_file() and model_dir() is not None


def pocket_installed() -> bool:
    return OMNIVOICE_PYTHON.is_file() and (POCKET_HF / "hub" / "models--kyutai--pocket-tts-without-voice-cloning").is_dir()


def pocket_cloning_weights():
    """Kyutai's gated voice-cloning weights, if the user downloaded them (HANDOFF 32 has the command)."""
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "pocket-tts" / "model.safetensors"
    return path if path.is_file() else None


def omnivoice_on() -> bool:
    return installed() and settings()["enabled"]


def elevenlabs_ready() -> bool:
    """There's an ElevenLabs key (its value stays inside the key resolver)."""
    from tools import tts_tool_providers
    try:
        tts_tool_providers._require_key("ELEVENLABS_API_KEY", "elevenlabs", "")
        return True
    except ValueError:
        return False


# Hermes extras behind the Edge and Piper voices. Stock Hermes installs an extra the first time something needs it
# (pm.ensure_import); JARVIS asks for them the same way right before using them, and its image prepares both at start
# (wiring.prepare_voice_extras) so the first fallback isn't a wait.
VOICE_EXTRAS = {"edge": "edge-tts", "piper": "piper"}


def ensure_voice_extra(engine: str) -> bool:
    """Install the Hermes extra an engine needs, if it's missing. Blocking (it may run uv), so call it off the loop."""
    extra = VOICE_EXTRAS.get(engine)
    if not extra:
        return True
    try:
        from pm import ensure_import
        ensure_import(extra)
        return True
    except Exception as exc:  # noqa: BLE001 — the caller falls back to the next voice
        logger.warning("Could not install the %s voice (%s): %s", engine, extra, exc)
        return False


def _importable(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available() -> dict:
    """engine id -> whether it can speak (as far as can be told without trying)."""
    return {
        "elevenlabs": elevenlabs_ready(),
        "pocket": pocket_installed() and pocket_settings()["enabled"],
        "omnivoice": omnivoice_on(),
        "edge": _importable("edge_tts"),
        "piper": _importable("piper"),
    }


def resolve(choice, avail: dict) -> str:
    """The engine to try first: the user's choice when it's a known engine, else the first available default."""
    if choice in ENGINES:
        return choice
    return next((e for e in DEFAULT_ORDER if avail.get(e)), "piper")


def voice_for(persona: str):
    """(wav, ref text) for a persona: the user's own clip when present, else the shipped voice."""
    from hermes_constants import get_hermes_home
    persona = persona if persona in PERSONAS else "jarvis"
    for folder in (get_hermes_home() / "vesper-voices", SHIPPED_VOICES):
        wav = folder / f"{persona}.wav"
        if wav.is_file():
            txt = folder / f"{persona}.txt"
            return wav, (txt.read_text(encoding="utf-8").strip() if txt.is_file() else "")
    raise FileNotFoundError(f"No voice clip for {persona}")


def own_clip(persona: str):
    """The user's own reference clip for *persona* ($HERMES_HOME/vesper-voices/<persona>.wav), or None."""
    from hermes_constants import get_hermes_home
    wav = get_hermes_home() / "vesper-voices" / f"{persona}.wav"
    return wav if wav.is_file() else None


def pack_voice(persona: str):
    """The Pocket TTS voice state for *persona* from the image's voice pack, or None. The pack ships encrypted;
    JARVIS_VOICE_KEY unlocks it into $HERMES_HOME/vesper-voices/pack at container start (image/cont-init.d). A state
    needs no cloning weights: the public Pocket TTS model speaks it exactly as the cloning model would."""
    from hermes_constants import get_hermes_home
    state = get_hermes_home() / "vesper-voices" / "pack" / f"{persona}.safetensors"
    return state if state.is_file() else None


def pack_installed() -> bool:
    return all(pack_voice(p) is not None for p in PERSONAS)


def cache_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "vesper-voice-cache"


def cache_key(wav: Path, ref_text: str, text: str, cfg: dict) -> str:
    """OmniVoice: same voice clip, same text, same model settings, same audio. The steps aren't part of it, so a line
    rendered carefully in the background is what a later request for it gets."""
    st = wav.stat()
    raw = json.dumps([CACHE_VERSION, str(wav), st.st_size, st.st_mtime_ns, ref_text, text, cfg["int8"]])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps([CACHE_VERSION, *parts]).encode()).hexdigest()[:40]


def prune_cache(limit: int = CACHE_LIMIT_BYTES):
    files = sorted((p for p in cache_dir().glob("*.*") if not p.name.startswith(".tmp-")), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > limit:
        p = files.pop(0)
        total -= p.stat().st_size
        with contextlib.suppress(OSError):
            p.unlink()


async def _cached(path: Path, render):
    """The bytes at *path* in the cache, made with ``await render(tmp_path)`` the first time."""
    if path.is_file():
        os.utime(path)
        return await asyncio.to_thread(path.read_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    os.close(fd)
    try:
        await render(tmp)
        if os.path.getsize(tmp) < 64:   # an empty answer must not be cached as the line's audio
            raise RuntimeError("no audio came back")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    await asyncio.to_thread(prune_cache)
    return await asyncio.to_thread(path.read_bytes)


class WorkerEngine:
    """One model in a worker process, one request at a time, started on demand and stopped when idle.
    A subclass says how to start it (load_settings, command, env) and what to ask it (synthesize)."""

    name = "worker"

    def __init__(self):
        self.proc = None
        self.lock = asyncio.Lock()
        self.waiting = 0          # foreground requests queued or running (prewarm yields to them)
        self.last_used = 0.0
        self.loading = False
        self.load_seconds = None
        self.hello = {}
        self.seq = 0
        self.reaper = None
        self.prewarm_queue = []
        self.prewarm_task = None
        self.cfg = None
        self.start_lock = asyncio.Lock()   # the dashboard's wake-up and a first request must not start two workers

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        async with self.start_lock:
            if not self.running:
                await self._start()

    async def _start(self):
        self.cfg = self.load_settings()
        self.loading = True
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
               "OMP_NUM_THREADS": str(self.cfg["threads"]), **self.env()}
        try:
            # command() runs python -I: isolated, so the worker's own folder (gateway/platforms, which has a
            # signal.py) can't shadow the standard library; that broke OmniVoice's first live start.
            self.proc = await asyncio.create_subprocess_exec(
                *self.command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, limit=1 << 20)
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=300)
            hello = json.loads(line or b"{}")
            if not hello.get("ready"):
                raise RuntimeError(f"{self.name} worker did not start")
            self.hello, self.load_seconds = hello, hello.get("load_seconds")
            logger.info("vesper voice: %s ready in %ss", self.name, self.load_seconds)
        except BaseException:
            await self.stop()
            raise
        finally:
            self.loading = False
        self.last_used = time.monotonic()
        if self.reaper is None or self.reaper.done():
            self.reaper = asyncio.ensure_future(self._reap())

    async def shutdown(self):
        """Stop the worker and the engine's background tasks (gateway or test shutdown)."""
        for task in (self.reaper, self.prewarm_task):
            if task is not None and not task.done():
                task.cancel()
        self.prewarm_queue.clear()
        await self.stop()

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _reap(self):
        while self.running:
            await asyncio.sleep(30)
            idle = (self.cfg or self.load_settings())["idle_minutes"] * 60
            if not self.lock.locked() and not self.waiting and time.monotonic() - self.last_used > idle:
                logger.info("vesper voice: %s idle, stopping it to free memory", self.name)
                await self.stop()

    async def _run(self, req: dict) -> dict:
        await self.start()
        self.seq += 1
        req = {"id": self.seq, **req}
        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=600)
        if not line:
            await self.stop()
            raise RuntimeError(f"{self.name} worker exited")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or f"{self.name} failed")
        self.last_used = time.monotonic()
        return reply

    async def _render(self, cached: Path, req: dict, still_wanted, background: bool):
        """The cached audio, or the worker's for *req* (serialized; skipped, None, once no longer wanted)."""
        if cached.is_file():
            os.utime(cached)
            return await asyncio.to_thread(cached.read_bytes)
        if not background:
            self.waiting += 1
        try:
            async with self.lock:
                if not still_wanted():
                    return None

                async def render(tmp):
                    await self._run({**req, "out": tmp})
                return await _cached(cached, render)   # also covers one rendered while this request waited
        finally:
            if not background:
                self.waiting -= 1

    def prewarm(self, persona: str, texts):
        """Render fixed lines while nothing else is waiting (a running one still finishes first)."""
        known = set(self.prewarm_queue)
        self.prewarm_queue += [(persona, t) for t in texts if (persona, t) not in known]
        if self.prewarm_task is None or self.prewarm_task.done():
            self.prewarm_task = asyncio.ensure_future(self._prewarm_loop())

    async def _prewarm_loop(self):
        while self.prewarm_queue:
            if self.waiting or self.lock.locked():
                await asyncio.sleep(2)
                continue
            persona, text = self.prewarm_queue.pop(0)
            try:
                await self.synthesize(text, persona, background=True)
            except Exception as exc:
                logger.info("vesper voice: prewarm skipped (%s)", exc)
                return

    def status(self) -> dict:
        return {"running": self.running, "loading": self.loading, "load_seconds": self.load_seconds,
                "queued": self.waiting, "prewarm": len(self.prewarm_queue)}


class OmniVoiceEngine(WorkerEngine):
    name = "omnivoice"

    def load_settings(self):
        return settings()

    def command(self):
        cfg = self.cfg or settings()
        return [str(omnivoice_python()), "-I", str(WORKER),
                json.dumps({"model": str(model_dir()),
                            **{k: cfg[k] for k in ("steps", "guidance", "int8", "threads", "device")}})]

    def env(self):
        return {"HF_HOME": str((omnivoice_root() or OMNIVOICE_ROOT) / "hf")}

    async def synthesize(self, text: str, persona: str, still_wanted=lambda: True, background=False):
        """WAV bytes for *text* in *persona*'s cloned voice, from the cache when possible. None if no longer wanted."""
        cfg = self.cfg or settings()
        wav, ref_text = await asyncio.to_thread(voice_for, persona)
        cached = cache_dir() / f"{cache_key(wav, ref_text, text, cfg)}.wav"
        req = {"text": text, "ref_wav": str(wav), "ref_text": ref_text, **quality_for(text, cfg, background)}
        return await self._render(cached, req, still_wanted, background)

    def status(self):
        return {**super().status(), "device": self.hello.get("device"),
                "settings": {k: (self.cfg or settings())[k] for k in ("steps", "guidance", "int8", "device")}}


class PocketEngine(WorkerEngine):
    name = "pocket"

    def load_settings(self):
        return pocket_settings()

    def command(self):
        cfg = self.cfg or pocket_settings()
        weights = pocket_cloning_weights()
        return [str(OMNIVOICE_PYTHON), "-I", str(POCKET_WORKER),
                json.dumps({"threads": cfg["threads"], "cloning_weights": str(weights) if weights else None})]

    def env(self):
        return {"HF_HOME": str(POCKET_HF)}

    def pick_voice(self, persona: str, cloning: bool):
        """(kind, voice) Pocket TTS speaks *persona* with: the user's own clip when it can clone, else the voice pack's
        state, else the shipped clip when it can clone, else the persona's catalog voice."""
        persona = persona if persona in PERSONAS else "jarvis"
        own = own_clip(persona)
        if cloning and own is not None:
            return "clip", own
        state = pack_voice(persona)
        if state is not None:
            return "pack", state
        if cloning:
            return "clip", voice_for(persona)[0]
        cfg = self.cfg or pocket_settings()
        return "catalog", cfg["voices"].get(persona, POCKET_DEFAULTS["voices"]["jarvis"])

    async def synthesize(self, text: str, persona: str, still_wanted=lambda: True, background=False):
        """WAV bytes for *text* in *persona*'s Pocket TTS voice (pick_voice)."""
        await self.start()   # whether it can clone is known once it's up
        kind, voice = await asyncio.to_thread(self.pick_voice, persona, bool(self.hello.get("cloning")))
        if kind == "catalog":
            voice, stamp = str(voice), 0
        else:
            voice, stamp = str(voice), voice.stat().st_mtime_ns
        cached = cache_dir() / f"{_key('pocket', voice, stamp, text)}.wav"
        return await self._render(cached, {"text": text, "voice": voice}, still_wanted, background)

    def status(self):
        cloning = bool(self.hello.get("cloning")) if self.hello else pocket_cloning_weights() is not None
        return {**super().status(), "cloning": bool(self.hello.get("cloning")),
                "voices": {p: self.pick_voice(p, cloning)[0] for p in PERSONAS}}


WORKERS = ("omnivoice", "pocket")
ENGINE = None          # the OmniVoice worker
POCKET_ENGINE = None   # the Pocket TTS worker


def engine():
    global ENGINE
    if ENGINE is None:
        ENGINE = OmniVoiceEngine()
    return ENGINE


def pocket_engine():
    global POCKET_ENGINE
    if POCKET_ENGINE is None:
        POCKET_ENGINE = PocketEngine()
    return POCKET_ENGINE


def worker_for(engine_id: str):
    return {"omnivoice": engine, "pocket": pocket_engine}[engine_id]()


async def shutdown():
    for eng in (ENGINE, POCKET_ENGINE):
        if eng is not None:
            await eng.shutdown()


def _wake(eng):
    if not eng.running and not eng.loading:
        task = asyncio.ensure_future(eng.start())
        task.add_done_callback(lambda t: t.exception() and logger.warning("vesper voice: %s failed to start: %s", eng.name, t.exception()))


async def voice_status(adapter, request):
    """GET /vesper/voice/status[?engine=<id>]: the engines and which can speak, the one this choice resolves to,
    and, for a NAS engine, wakes it so the first reply doesn't wait for the model to load."""
    error = adapter._check_auth(request)
    if error is not None:
        return error
    avail = await asyncio.to_thread(available)
    chosen = resolve(request.query.get("engine"), avail)
    body = {"engine": chosen, "default": resolve(None, avail),
            "engines": [{"id": k, **v, "available": bool(avail.get(k))} for k, v in ENGINES.items()]}
    if chosen in WORKERS and avail.get(chosen):
        eng = worker_for(chosen)
        _wake(eng)
        body.update(eng.status())
    return web.json_response(body)


_PREWARM_MAX = 30


async def voice_prewarm(adapter, request):
    """POST /vesper/voice/prewarm {persona, texts: [...], engine}: render the dashboard's fixed lines ahead of time.
    Only OmniVoice needs it: Pocket TTS is quick, and the online engines would spend characters on lines that may
    never be said."""
    from gateway.platforms import api_server
    error = adapter._check_auth(request)
    if error is not None:
        return error
    try:
        body = await request.json()
    except (ValueError, LookupError):
        return api_server._error_response("Invalid JSON body", 400, code="invalid_json")
    persona = body.get("persona") if isinstance(body, dict) else None
    texts = body.get("texts") if isinstance(body, dict) else None
    if persona not in PERSONAS or not isinstance(texts, list):
        return api_server._error_response("Expected {persona, texts: [...]}", 400, code="invalid_prewarm")
    texts = [" ".join(t.split())[:300] for t in texts if isinstance(t, str) and t.strip()][:_PREWARM_MAX]
    avail = await asyncio.to_thread(available)
    chosen = resolve(body.get("engine"), avail)
    if chosen != "omnivoice" or not avail["omnivoice"]:
        return web.json_response({"queued": 0, "engine": chosen})
    engine().prewarm(persona, texts)
    return web.json_response({"queued": len(texts), "engine": chosen})


_PIPER_VOICE = re.compile(r"[a-z]{2,3}_[A-Z]{2}-[a-z0-9_]+-(?:x_low|low|medium|high)")


async def _elevenlabs(text, persona, voices, still_wanted):
    """The persona's ElevenLabs voice: the one picked for it in the setup guide (tts.elevenlabs.persona_voices,
    api_server_setup_voice), else its id in personas.js. If the account doesn't have that voice (someone else's
    voice, deleted, or a retired premade one), the account's configured voice before giving up on ElevenLabs."""
    from tools import tts_tool
    from .setup_voice import persona_voices
    cfg = _tts_config()
    configured = (cfg.get("elevenlabs") or {}).get("voice_id")
    picked = persona_voices().get(persona)
    failure = None
    for voice_id in dict.fromkeys(v for v in (picked, voices.get("voice_id"), configured) if v) or [None]:
        attempt = copy.deepcopy(cfg)
        el = attempt.setdefault("elevenlabs", {})
        if voice_id:
            el["voice_id"] = voice_id
        cached = cache_dir() / f"{_key('elevenlabs', el.get('voice_id'), el.get('model_id'), text)}.mp3"
        try:
            return await _cached(cached, lambda tmp: asyncio.to_thread(tts_tool._generate_elevenlabs, text, tmp, attempt)), "audio/mpeg"
        except Exception as exc:
            failure = exc
            logger.info("vesper voice: ElevenLabs voice %s failed (%s)", voice_id, type(exc).__name__)
    raise failure


async def _edge(text, persona, voices, still_wanted):
    await asyncio.to_thread(ensure_voice_extra, "edge")
    from tools import tts_tool_providers
    cfg = copy.deepcopy(_tts_config())
    if voices.get("edge_voice"):
        cfg.setdefault("edge", {})["voice"] = voices["edge_voice"]
    cached = cache_dir() / f"{_key('edge', (cfg.get('edge') or {}).get('voice'), text)}.mp3"
    return await _cached(cached, lambda tmp: tts_tool_providers._generate_edge_tts(text, tmp, cfg)), "audio/mpeg"


async def _piper(text, persona, voices, still_wanted):
    await asyncio.to_thread(ensure_voice_extra, "piper")
    from tools.tts_tool_local import _generate_piper_tts
    local = copy.deepcopy(_tts_config())
    piper_cfg = local.setdefault("piper", {})
    if Path("/opt/piper-voices").is_dir() and not piper_cfg.get("voices_dir"):
        piper_cfg["voices_dir"] = "/opt/piper-voices"
    # The persona's Piper voice; else the configured one if it is actually installed (a missing one would try to
    # download, and the NAS can't reach the download host); else the shipped default.
    configured = piper_cfg.get("voice") or ""
    shipped = Path(piper_cfg.get("voices_dir") or "/opt/piper-voices") / f"{configured}.onnx"
    piper_voice = voices.get("piper_voice") or ""
    piper_cfg["voice"] = (piper_voice if _PIPER_VOICE.fullmatch(piper_voice)
                          else configured if configured and shipped.is_file() else "en_GB-alan-medium")
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        await asyncio.to_thread(_generate_piper_tts, text, tmp, local)
        return await asyncio.to_thread(Path(tmp).read_bytes), "audio/wav"
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


async def _worker(name, text, persona, voices, still_wanted):
    audio = await worker_for(name).synthesize(text, persona, still_wanted=still_wanted)
    return None if audio is None else (audio, "audio/wav")


# engine id -> async (text, persona, voices, still_wanted) -> (bytes, content type), or None when no longer wanted
RENDERERS = {"elevenlabs": _elevenlabs, "pocket": partial(_worker, "pocket"), "omnivoice": partial(_worker, "omnivoice"),
             "edge": _edge, "piper": _piper}
# What X-Vesper-Voice says spoke; "local" for Piper, as before there were several local engines.
SPOKEN_AS = {"piper": "local"}


async def speak(request, text: str, persona: str, choice=None, **voices):
    """POST /vesper/tts: (bytes, content type, which engine spoke), or None when the browser left while it waited.
    The chosen engine first, then its fallbacks, then Piper. *voices*: voice_id (ElevenLabs), edge_voice,
    piper_voice, the persona's voices from personas.js. Raises when even Piper fails."""
    still_wanted = lambda: request.transport is not None and not request.transport.is_closing()
    avail = await asyncio.to_thread(available)
    first = resolve(choice, avail)
    order = [e for e in dict.fromkeys([first, *FALLBACKS[first], "piper"]) if e == "piper" or avail.get(e)]
    failure = None
    for name in order:
        try:
            spoken = await RENDERERS[name](text, persona, voices, still_wanted)
        except Exception as exc:
            failure = exc
            logger.warning("vesper voice: %s failed (%s: %s), trying the next voice", name, type(exc).__name__, str(exc)[:200])
            continue
        if spoken is None:
            return None
        return (*spoken, SPOKEN_AS.get(name, name))
    raise RuntimeError(f"every voice failed, last: {failure}")
