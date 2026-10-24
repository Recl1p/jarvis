"""OmniVoice, the optional voice engine JARVIS installs on request (the setup screen's OmniVoice card).

The image ships Pocket TTS. OmniVoice (k2-fsa) clones each persona's voice from its reference clip; it is much heavier
(about 9 GB, 14 GB with NVIDIA's CUDA libraries) and, on a CPU, several times slower than real time, so it isn't in the
published image: whoever wants it installs it here, into the data volume (config.OMNIVOICE_DIR), where it survives
image updates. Same pins as an image build with OmniVoice (deployment/omnivoice-requirements.txt, copied into every
image), with the CPU build of torch or the CUDA 12.8 build of the same version. The connector (vesper_voice.py) uses it
once the marker file is written, and runs it on the GPU when torch sees one.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

from . import config, state
from .managed import LogBuffer, stream_process

logger = logging.getLogger(__name__)

MODEL_REPO = "k2-fsa/OmniVoice"
MODEL_REVISION = "c5fdb5ccb189668d56333f77ba2629f4cd7535f4"
MARKER = ".installed"
VARIANTS = {
    "cpu": {"label": "CPU", "index": "https://download.pytorch.org/whl/cpu", "suffix": "+cpu",
            "size_gb": 9, "needs_gb": 12},
    "cuda": {"label": "NVIDIA GPU", "index": "https://download.pytorch.org/whl/cu128", "suffix": "+cu128",
             "size_gb": 14, "needs_gb": 18},
}
_TORCH_PIN = re.compile(r"^(torch|torchaudio)==([0-9.]+)\+cpu\s*$", re.M)


def in_image() -> bool:
    """This image was built with OmniVoice (JARVIS_LOCAL_VOICES=1): nothing to install."""
    return (config.OMNIVOICE_IMAGE_DIR / "hf" / "hub" / "models--k2-fsa--OmniVoice").is_dir()


def installed() -> Optional[dict]:
    """What the setup screen installed ({"variant", "torch", "cuda", "installed_at"}), or None."""
    try:
        info = json.loads((config.OMNIVOICE_DIR / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (config.OMNIVOICE_DIR / "venv" / "bin" / "python").exists():
        return None
    return info if isinstance(info, dict) else {}


def gpus() -> list:
    """The NVIDIA GPUs this container can use (nvidia-smi is there only when the container was given one)."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split(" (UUID")[0].strip() for line in out.splitlines() if line.startswith("GPU ")]


def requirements_for(variant: str) -> str:
    """The image's OmniVoice lock, with torch and torchaudio switched to *variant*'s build of the same version."""
    text = config.OMNIVOICE_REQUIREMENTS.read_text(encoding="utf-8")
    if not _TORCH_PIN.search(text):
        raise RuntimeError(f"{config.OMNIVOICE_REQUIREMENTS} doesn't pin torch's CPU build; is this a JARVIS image?")
    suffix = VARIANTS[variant]["suffix"]
    return _TORCH_PIN.sub(lambda m: f"{m.group(1)}=={m.group(2)}{suffix}", text)


def free_gb() -> int:
    target = config.OMNIVOICE_DIR if config.OMNIVOICE_DIR.exists() else config.OMNIVOICE_DIR.parent
    return shutil.disk_usage(target).free // 1024 ** 3


class OmniVoiceJob:
    """Install or remove OmniVoice in the data volume. One at a time; the setup screen follows the log."""

    def __init__(self):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.log = LogBuffer(config.STATE_DIR / "omnivoice.log")
        self.status = "idle"      # idle | running | done | failed
        self.step = ""
        self.error = ""
        self._task: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict:
        return {"in_image": in_image(), "installed": installed(), "gpus": gpus(), "free_gb": free_gb(),
                "variants": {k: {"label": v["label"], "size_gb": v["size_gb"]} for k, v in VARIANTS.items()},
                "busy": self.busy, "status": self.status, "step": self.step, "error": self.error}

    def start_install(self, variant: str) -> bool:
        if self.busy:
            return False
        self.status, self.error, self.step = "running", "", ""
        self._task = asyncio.create_task(self._install(variant))
        return True

    def start_remove(self) -> bool:
        if self.busy:
            return False
        self.status, self.error, self.step = "running", "", ""
        self._task = asyncio.create_task(self._remove())
        return True

    def _step(self, name: str) -> None:
        self.step = name
        self.log.write(f"== {name}")

    def _env(self) -> dict:
        root = config.OMNIVOICE_DIR
        return {**os.environ, "HOME": str(config.HERMES_HOME), "UV_PYTHON_INSTALL_DIR": str(root / "python"),
                "UV_CACHE_DIR": str(root / ".uv-cache"), "HF_HOME": str(root / "hf"), "HF_HUB_DISABLE_TELEMETRY": "1",
                "UV_NO_PROGRESS": "1"}

    async def _run(self, argv: list) -> None:
        code = await stream_process(argv, self.log, env=self._env(), cwd=str(config.HERMES_HOME))
        if code != 0:
            raise RuntimeError(f"{os.path.basename(argv[0])} stopped with exit code {code}; see the log above.")

    async def _install(self, variant: str) -> None:
        root = config.OMNIVOICE_DIR
        python = root / "venv" / "bin" / "python"
        try:
            if variant not in VARIANTS:
                raise RuntimeError(f"Unknown OmniVoice build {variant!r}.")
            spec = VARIANTS[variant]
            if in_image():
                raise RuntimeError("This JARVIS image already has OmniVoice built in.")
            self._step(f"Checking free space ({spec['label']} build, about {spec['size_gb']} GB)")
            if root.exists():
                # A previous install (another build, or one that stopped half-way): start clean.
                self.log.write(f"Removing the previous OmniVoice in {root}")
                await asyncio.to_thread(shutil.rmtree, root, True)
            free = free_gb()
            if free < spec["needs_gb"]:
                raise RuntimeError(f"Only {free} GB free in {config.HERMES_HOME}; the {spec['label']} build of "
                                   f"OmniVoice needs about {spec['needs_gb']} GB while it installs.")
            self.log.write(f"{free} GB free")
            root.mkdir(parents=True)
            requirements = root / "requirements.txt"
            requirements.write_text(requirements_for(variant), encoding="utf-8")

            self._step("Getting Python 3.12")
            await self._run([config.UV, "venv", "--python", "3.12", str(root / "venv")])

            self._step(f"Installing OmniVoice and torch ({spec['label']} build); the biggest step")
            await self._run([config.UV, "pip", "install", "--no-cache", "--python", str(python),
                             "--index-strategy", "unsafe-best-match", "--extra-index-url", spec["index"],
                             "-r", str(requirements)])

            self._step("Downloading the OmniVoice model")
            await self._run([str(python), "-c", "from huggingface_hub import snapshot_download; "
                             f"snapshot_download({MODEL_REPO!r}, revision={MODEL_REVISION!r})"])

            self._step("Checking it")
            probe = ("import json, torch, omnivoice; print('JARVIS-PROBE ' + json.dumps("
                     "{'torch': torch.__version__, 'cuda': torch.cuda.is_available(), "
                     "'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}))")
            proc = await asyncio.create_subprocess_exec(str(python), "-c", probe, env=self._env(),
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            out = (await proc.communicate())[0].decode("utf-8", "replace")
            line = next((x for x in out.splitlines() if x.startswith("JARVIS-PROBE ")), "")
            if proc.returncode != 0 or not line:
                for x in out.strip().splitlines()[-8:]:
                    self.log.write(f"   {x}")
                raise RuntimeError("OmniVoice is installed but doesn't start; see the log above.")
            probe_info = json.loads(line[len("JARVIS-PROBE "):])
            self.log.write(f"torch {probe_info['torch']}, " + (f"running on {probe_info['gpu']}" if probe_info["cuda"]
                                                               else "running on the CPU"))
            if variant == "cuda" and not probe_info["cuda"]:
                self.log.write("No GPU is visible to this container right now: OmniVoice uses the CPU until it is "
                               "(INSTALL.md, OmniVoice).")
            for leftover in (root / ".uv-cache", root / "hf" / "xet"):
                await asyncio.to_thread(shutil.rmtree, leftover, True)
            info = {"variant": variant, "torch": probe_info["torch"], "cuda": probe_info["cuda"],
                    "gpu": probe_info["gpu"], "installed_at": state.now()}
            (root / MARKER).write_text(json.dumps(info), encoding="utf-8")
            self.step = "Done"
            self.log.write("== OmniVoice is installed. Choose it under VOICE in the dashboard's Vitals panel.")
            self.status = "done"
        except Exception as exc:  # noqa: BLE001 — every failure is shown on the setup screen
            logger.exception("OmniVoice install failed")
            self.error = str(exc)
            self.log.write(f"!! {exc}")
            self.status = "failed"

    async def _remove(self) -> None:
        try:
            self._step("Removing OmniVoice")
            # The marker first: from then on the dashboard no longer offers OmniVoice, whatever the rest takes.
            (config.OMNIVOICE_DIR / MARKER).unlink(missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, config.OMNIVOICE_DIR, True)
            self.log.write(f"Removed {config.OMNIVOICE_DIR}; {free_gb()} GB free now.")
            self.step = "Done"
            self.status = "done"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.log.write(f"!! {exc}")
            self.status = "failed"
