"""Pocket TTS worker for the dashboard's fast local voice (api_server_vesper_voice.py starts it; not imported by Hermes).

Kyutai's Pocket TTS (100M parameters, CPU): about three times faster than real time on the NAS (HANDOFF 32), where
OmniVoice is several times slower. Runs under the image's voice interpreter (/opt/omnivoice/venv, which has Pocket TTS
too) with its public weights baked into /opt/pocket-tts/hf, and speaks the same JSON-lines protocol as the OmniVoice
worker:

    -> {"id": 1, "text": "...", "voice": "peter_yearsley" | "/path/clip.wav", "out": "/path/out.wav"}
    <- {"id": 1, "ok": true, "seconds": 3.4, "took": 1.2}      or      {"id": 1, "ok": false, "error": "..."}

A voice is one of Pocket TTS's own catalog voices, or a WAV clip to clone. Cloning needs Kyutai's gated weights, which
the image doesn't have: settings {"cloning_weights": "/path/model.safetensors"} (a copy the user downloaded after
accepting Kyutai's terms) turns it on; without them a clip request fails and the caller uses the catalog voice.
Library chatter goes to stderr; stdout carries only the protocol.
"""

import json
import os
import sys
import tempfile
import time


def tidy(audio, rate=24000):
    """Trim silence at the ends (keeping 150 ms) and bring the peak to 0.9."""
    import numpy as np
    audio = np.asarray(audio, dtype="float32")
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak < 1e-4:
        return audio
    frame = rate // 100   # 10 ms
    n = audio.size // frame
    if n:
        energy = np.abs(audio[: n * frame]).reshape(n, frame).max(axis=1)
        loud = np.nonzero(energy > max(1e-3, 0.015 * peak))[0]
        if loud.size:
            pad = 15 * frame
            audio = audio[max(0, loud[0] * frame - pad): min(audio.size, (loud[-1] + 1) * frame + pad)]
    return audio * (0.9 / peak)


def load(settings):
    from pocket_tts import TTSModel
    weights = settings.get("cloning_weights")
    if not weights:
        return TTSModel.load_model()
    # The packaged English config with its gated weights swapped for the user's local copy.
    import pocket_tts
    import yaml
    from pathlib import Path
    config = yaml.safe_load((Path(pocket_tts.__file__).parent / "config" / "english.yaml").read_text(encoding="utf-8"))
    config["weights_path"] = str(weights)
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f)
    try:
        return TTSModel.load_model(config=path)
    finally:
        os.remove(path)


def main():
    settings = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)   # anything a library prints lands on stderr, never in the protocol
    try:
        os.nice(int(settings.get("nice", 5)))
    except OSError:
        pass
    import soundfile as sf
    import torch

    torch.set_num_threads(int(settings.get("threads", 3)))
    started = time.time()
    model = load(settings)
    proto.write(json.dumps({"ready": True, "load_seconds": round(time.time() - started, 1),
                            "cloning": bool(model.has_voice_cloning)}) + "\n")
    states = {}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        reply = {"id": req.get("id")}
        try:
            voice = req["voice"]
            key = (voice, os.stat(voice).st_mtime_ns) if voice.startswith("/") else (voice, 0)
            if key not in states:
                states[key] = model.get_state_for_audio_prompt(voice)
            t = time.time()
            clip = tidy(model.generate_audio(states[key], req["text"]).numpy(), model.sample_rate)
            if clip.size < model.sample_rate // 10:
                raise RuntimeError("Pocket TTS produced no audio")
            sf.write(req["out"], clip, model.sample_rate)
            reply.update(ok=True, seconds=round(clip.size / model.sample_rate, 2), took=round(time.time() - t, 2))
        except Exception as exc:   # one bad request must not take the worker down
            reply.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        proto.write(json.dumps(reply) + "\n")


if __name__ == "__main__":
    main()
