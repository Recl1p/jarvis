"""Where the dashboard finds OmniVoice (built into the image, or installed into the data volume from the setup screen,
complete only once its marker is written) and which device its worker runs on."""

import json
import sys
from pathlib import Path

import pytest

import tools.tts_tool as tts_tool
from jarvis import vesper_voice as voice

sys.path.insert(0, str(Path(voice.WORKER).parent))
import omnivoice_worker  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(voice, "OMNIVOICE_ROOT", tmp_path / "image-omnivoice")   # an image without OmniVoice
    return tmp_path / "home"


def install_into(root: Path, marker=True):
    snap = root / "hf" / "hub" / "models--k2-fsa--OmniVoice" / "snapshots" / "c5fdb5cc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    if marker:
        (root / voice.OMNIVOICE_MARKER).write_text(json.dumps({"variant": "cpu"}))
    return snap


def test_not_installed_anywhere(home):
    assert voice.omnivoice_root() is None and voice.omnivoice_python() is None
    assert voice.model_dir() is None and not voice.installed()


def test_installed_from_the_setup_screen_counts_only_when_finished(home):
    root = home / "omnivoice"
    snap = install_into(root, marker=False)
    assert not voice.installed()                       # an install still running (or one that failed half-way)
    (root / voice.OMNIVOICE_MARKER).write_text("{}")
    assert voice.omnivoice_root() == root and voice.model_dir() == snap and voice.installed()
    assert voice.omnivoice_python() == root / "venv" / "bin" / "python"


def test_the_image_comes_first(home):
    install_into(home / "omnivoice")
    image_snap = install_into(voice.OMNIVOICE_ROOT, marker=False)   # an image build has no marker
    assert voice.omnivoice_root() == voice.OMNIVOICE_ROOT and voice.model_dir() == image_snap


def test_the_worker_gets_its_interpreter_model_and_device(home, monkeypatch):
    root = home / "omnivoice"
    snap = install_into(root)
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"omnivoice": {"device": "cuda", "steps": 6}})
    engine = voice.OmniVoiceEngine()
    command = engine.command()
    assert command[0] == str(root / "venv" / "bin" / "python")
    settings = json.loads(command[-1])
    assert settings["model"] == str(snap) and settings["device"] == "cuda" and settings["steps"] == 6
    assert engine.env() == {"HF_HOME": str(root / "hf")}


def test_device_setting_is_validated(home, monkeypatch):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"omnivoice": {"device": "tpu"}})
    assert voice.settings()["device"] == "auto"
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    assert voice.settings()["device"] == "auto"


def test_the_worker_uses_the_gpu_only_when_there_is_one():
    assert omnivoice_worker.pick_device("auto", True) == "cuda"
    assert omnivoice_worker.pick_device("auto", False) == "cpu"
    assert omnivoice_worker.pick_device("cuda", False) == "cpu"     # no GPU: still speaks, on the CPU
    assert omnivoice_worker.pick_device("cpu", True) == "cpu"
