"""OmniVoice worker for the dashboard's voice (api_server_vesper_voice.py starts it; not imported by Hermes).

Runs under the image's own OmniVoice interpreter (/opt/omnivoice/venv, Python 3.12 + CPU torch), loads the model
once and then answers one request per line on stdin with one JSON line on stdout:

    -> {"id": 1, "text": "...", "ref_wav": "/path/voice.wav", "ref_text": "what the clip says", "out": "/path/out.wav",
        "steps": 16, "guidance": 2.0}      (steps / guidance optional: override the defaults for this request)
    <- {"id": 1, "ok": true, "seconds": 3.4, "took": 9.8}      or      {"id": 1, "ok": false, "error": "..."}

Settings come as argv JSON: {"steps": 8, "guidance": 0.0, "int8": false, "threads": 3, "device": "auto"}. "auto" runs
on an NVIDIA GPU when torch sees one (the setup screen's GPU build of OmniVoice), else on the CPU; the ready line says
which. Every request clones the voice
in its reference clip (a clone prompt per clip is built once and kept), so a persona always sounds the same.
Library chatter goes to stderr; stdout carries only the protocol.
"""

import json
import os
import sys
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
        loud = np.nonzero(energy > max(1e-3, 0.015 * peak))[0]   # low: soft word endings must survive
        if loud.size:
            pad = 15 * frame
            audio = audio[max(0, loud[0] * frame - pad): min(audio.size, (loud[-1] + 1) * frame + pad)]
    return audio * (0.9 / peak)


def pick_device(wanted: str, cuda_available: bool) -> str:
    """cuda when asked for (or auto) and torch sees a GPU; the CPU otherwise, so a missing GPU never stops the voice."""
    return "cuda" if wanted in ("auto", "cuda") and cuda_available else "cpu"


def main():
    settings = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)   # anything a library prints lands on stderr, never in the protocol
    try:
        os.nice(int(settings.get("nice", 5)))   # the gateway and the NAS's other apps come first
    except OSError:
        pass
    import soundfile as sf
    import torch
    from omnivoice import OmniVoice
    from omnivoice.models.omnivoice import OmniVoiceGenerationConfig

    torch.set_num_threads(int(settings.get("threads", 3)))
    started = time.time()
    device = pick_device(settings.get("device", "auto"), torch.cuda.is_available())
    model = OmniVoice.from_pretrained(settings.get("model", "k2-fsa/OmniVoice"),
                                      device_map="cuda:0" if device == "cuda" else "cpu",
                                      dtype=torch.float16 if device == "cuda" else torch.float32)
    if settings.get("int8", False) and device == "cpu":
        # Dynamic int8 for the language model's Linear layers: faster on a CPU, but it garbles words (HANDOFF 31).
        model.llm = torch.ao.quantization.quantize_dynamic(model.llm, {torch.nn.Linear}, dtype=torch.qint8)
    # OmniVoice's own post-processing trims short lines ("On it.", "Good evening.") down to nothing with these voices,
    # so it's off and tidy() below does a gentler version.
    config = OmniVoiceGenerationConfig(num_step=int(settings.get("steps", 8)), guidance_scale=float(settings.get("guidance", 0.0)),
                                       postprocess_output=False)
    proto.write(json.dumps({"ready": True, "load_seconds": round(time.time() - started, 1), "device": device}) + "\n")
    prompts = {}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        reply = {"id": req.get("id")}
        try:
            key = (req["ref_wav"], os.stat(req["ref_wav"]).st_mtime_ns, req.get("ref_text") or "")
            if key not in prompts:
                prompts[key] = model.create_voice_clone_prompt(ref_audio=req["ref_wav"], ref_text=req.get("ref_text") or None)
            t = time.time()
            cfg = config
            if req.get("steps") or req.get("guidance") is not None:   # per request: short lines need more care
                cfg = OmniVoiceGenerationConfig(num_step=int(req.get("steps") or config.num_step),
                                                guidance_scale=float(config.guidance_scale if req.get("guidance") is None else req["guidance"]),
                                                postprocess_output=False)
            audio = model.generate(text=req["text"], voice_clone_prompt=prompts[key], generation_config=cfg)
            clip = tidy(audio[0])
            if clip.size < 2400:   # under 0.1 s: nothing usable came out
                raise RuntimeError("OmniVoice produced no audio")
            sf.write(req["out"], clip, 24000)
            reply.update(ok=True, seconds=round(clip.size / 24000, 2), took=round(time.time() - t, 2))
        except Exception as exc:   # one bad request must not take the worker down
            reply.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        proto.write(json.dumps(reply) + "\n")


if __name__ == "__main__":
    main()
