"""Installing OmniVoice from the setup screen: the pins it installs with (CPU or CUDA torch), the steps with a stand-in
uv and Python, failures, reinstalling another build, removing it, GPU detection and the setup API."""

import asyncio
import os
import shutil
import stat
import textwrap
from pathlib import Path

import aiohttp
import pytest

from jarvis_launcher import auth, config, server, state, voices

ROOT = Path(__file__).resolve().parents[2]
H = {"X-JARVIS": "1"}
KEY = "k" * 64
PASSWORD = "correct horse battery"

FAKE_PYTHON = textwrap.dedent('''\
    #!/bin/sh
    # Stand-in for the venv's Python: the model download and the probe the installer runs.
    case "$2" in
      *snapshot_download*) mkdir -p "$HF_HOME/hub/models--k2-fsa--OmniVoice/snapshots/c5fdb5cc"
                           echo "{}" > "$HF_HOME/hub/models--k2-fsa--OmniVoice/snapshots/c5fdb5cc/config.json"
                           mkdir -p "$HF_HOME/xet"; echo fetched ;;
      *JARVIS-PROBE*) echo "loading torch"; echo 'JARVIS-PROBE {"torch": "2.8.0+cpu", "cuda": false, "gpu": ""}' ;;
    esac
    ''')
FAKE_UV = textwrap.dedent('''\
    #!/bin/sh
    # Stand-in for uv: "venv" makes the venv with the Python above, "pip install" records its arguments.
    echo "uv $*" >> "$FAKE_UV_LOG"
    if [ "$1" = venv ]; then
      mkdir -p "$4/bin"; cp "$FAKE_PYTHON" "$4/bin/python"; chmod +x "$4/bin/python"; mkdir -p "$UV_PYTHON_INSTALL_DIR"
    elif [ "$1" = pip ]; then
      [ -n "$FAKE_UV_FAIL" ] && { echo "error: no matching distribution"; exit 2; }
      mkdir -p "$UV_CACHE_DIR"; echo "Installed 90 packages"
    fi
    ''')


def executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def fakes(scratch, monkeypatch):
    tools = scratch / "tools"
    shutil.rmtree(tools, ignore_errors=True)
    tools.mkdir()
    monkeypatch.setenv("FAKE_PYTHON", str(executable(tools / "python", FAKE_PYTHON)))
    monkeypatch.setenv("FAKE_UV_LOG", str(tools / "uv.log"))
    monkeypatch.delenv("FAKE_UV_FAIL", raising=False)
    monkeypatch.setattr(config, "UV", str(executable(tools / "uv", FAKE_UV)))
    requirements = tools / "requirements.txt"
    shutil.copy(ROOT / "deployment" / "omnivoice-requirements.txt", requirements)
    monkeypatch.setattr(config, "OMNIVOICE_REQUIREMENTS", requirements)
    shutil.rmtree(scratch / "image-omnivoice", ignore_errors=True)
    monkeypatch.setattr(config, "OMNIVOICE_IMAGE_DIR", scratch / "image-omnivoice")
    return lambda: (tools / "uv.log").read_text().splitlines() if (tools / "uv.log").exists() else []


async def finish(job):
    for _ in range(200):
        if not job.busy:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the job didn't finish")


def test_requirements_switch_only_torch_to_the_cuda_build(fakes):
    cpu = voices.requirements_for("cpu")
    cuda = voices.requirements_for("cuda")
    assert "torch==2.8.0+cpu" in cpu and "torchaudio==2.8.0+cpu" in cpu
    assert "torch==2.8.0+cu128" in cuda and "torchaudio==2.8.0+cu128" in cuda and "+cpu" not in cuda
    assert [l for l in cpu.splitlines() if "torch" not in l] == [l for l in cuda.splitlines() if "torch" not in l]
    assert "omnivoice==" in cuda


async def test_cpu_install_end_to_end(fakes):
    job = voices.OmniVoiceJob()
    assert job.start_install("cpu") and not job.start_install("cpu")       # one at a time
    await finish(job)
    assert job.status == "done", job.log.lines
    root = config.OMNIVOICE_DIR
    info = voices.installed()
    assert info["variant"] == "cpu" and info["torch"] == "2.8.0+cpu" and info["cuda"] is False
    assert (root / "hf" / "hub" / "models--k2-fsa--OmniVoice" / "snapshots" / "c5fdb5cc" / "config.json").is_file()
    assert not (root / ".uv-cache").exists() and not (root / "hf" / "xet").exists()   # cleaned up
    calls = fakes()
    assert calls[0] == f"uv venv --python 3.12 {root / 'venv'}"
    assert "https://download.pytorch.org/whl/cpu" in calls[1] and f"-r {root / 'requirements.txt'}" in calls[1]
    assert "torch==2.8.0+cpu" in (root / "requirements.txt").read_text()
    snap = job.snapshot()
    assert snap["installed"]["variant"] == "cpu" and snap["status"] == "done" and not snap["busy"]


async def test_a_failed_install_leaves_no_marker(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_UV_FAIL", "1")
    job = voices.OmniVoiceJob()
    job.start_install("cpu")
    await finish(job)
    assert job.status == "failed" and "exit code 2" in job.error
    assert voices.installed() is None
    assert any("no matching distribution" in line for line in job.log.lines)


async def test_installing_the_gpu_build_replaces_the_cpu_one(fakes):
    job = voices.OmniVoiceJob()
    job.start_install("cpu")
    await finish(job)
    (config.OMNIVOICE_DIR / "stale-file").write_text("x")
    job.start_install("cuda")
    await finish(job)
    assert job.status == "done", job.log.lines
    assert voices.installed()["variant"] == "cuda"
    assert not (config.OMNIVOICE_DIR / "stale-file").exists()
    assert "https://download.pytorch.org/whl/cu128" in fakes()[-1]
    assert "torch==2.8.0+cu128" in (config.OMNIVOICE_DIR / "requirements.txt").read_text()
    assert any("No GPU is visible" in line for line in job.log.lines)     # the probe saw no GPU


async def test_remove_frees_the_space(fakes):
    job = voices.OmniVoiceJob()
    job.start_install("cpu")
    await finish(job)
    job.start_remove()
    await finish(job)
    assert job.status == "done" and not config.OMNIVOICE_DIR.exists() and voices.installed() is None


async def test_no_install_over_an_image_that_has_omnivoice(fakes):
    (config.OMNIVOICE_IMAGE_DIR / "hf" / "hub" / "models--k2-fsa--OmniVoice").mkdir(parents=True)
    job = voices.OmniVoiceJob()
    job.start_install("cpu")
    await finish(job)
    assert job.status == "failed" and "already has OmniVoice" in job.error and fakes() == []


async def test_not_enough_space(fakes, monkeypatch):
    monkeypatch.setattr(voices, "free_gb", lambda: 3)
    job = voices.OmniVoiceJob()
    job.start_install("cuda")
    await finish(job)
    assert job.status == "failed" and "needs about 18 GB" in job.error and fakes() == []


def test_gpus_come_from_nvidia_smi(scratch, monkeypatch):
    bin_dir = scratch / "bin"
    shutil.rmtree(bin_dir, ignore_errors=True)
    bin_dir.mkdir()
    executable(bin_dir / "nvidia-smi", "#!/bin/sh\necho 'GPU 0: NVIDIA GeForce RTX 3060 (UUID: GPU-1234)'\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert voices.gpus() == ["GPU 0: NVIDIA GeForce RTX 3060"]


@pytest.fixture
async def client(aiohttp_client):
    auth.set_password(PASSWORD)
    return await aiohttp_client(server.build_app(),
                                headers={"Authorization": aiohttp.BasicAuth("jarvis", PASSWORD).encode()})


async def test_setup_api(client, fakes):
    data = await (await client.get("/jarvis/api/voices")).json()
    assert data["managed"] is False and data["omnivoice"]["installed"] is None
    assert set(data["omnivoice"]["variants"]) == {"cpu", "cuda"}
    resp = await client.post("/jarvis/api/voices/omnivoice", json={"variant": "cpu"}, headers=H)
    assert resp.status == 409 and (await resp.json())["error"]["code"] == "not_managed"
    state.update(mode="managed")
    (config.HERMES_HOME / ".env").write_text(f"API_SERVER_KEY={KEY}\n")
    keyed = {**H, "Authorization": f"Bearer {KEY}"}
    assert (await client.post("/jarvis/api/voices/omnivoice", json={"variant": "cpu"}, headers=H)).status == 401
    assert (await client.post("/jarvis/api/voices/omnivoice", json={"variant": "tpu"}, headers=keyed)).status == 400
    assert (await client.post("/jarvis/api/voices/omnivoice", json={"variant": "cpu"}, headers=keyed)).status == 202
    for _ in range(200):
        status = (await (await client.get("/jarvis/api/voices")).json())["omnivoice"]
        if not status["busy"]:
            break
        await asyncio.sleep(0.05)
    assert status["status"] == "done" and status["installed"]["variant"] == "cpu"
    log = await (await client.get("/jarvis/api/log?which=omnivoice&offset=0")).json()
    assert any(line.startswith("== Downloading the OmniVoice model") for line in log["lines"])
    assert (await client.post("/jarvis/api/voices/omnivoice/remove", json={}, headers=keyed)).status == 202
