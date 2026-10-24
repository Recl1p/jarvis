# JARVIS: the dashboard, voice and browser desktop for Hermes Agent. Hermes itself is NOT in this image.
#
# On first start the setup screen (http://<host>:8642) asks whether to install Hermes (Nous Research's official
# installer, run into the data volume only after the user agrees) or to connect a Hermes the user already runs.
# Either way the "jarvis" connector plugin from connector/ goes into that Hermes and serves the dashboard at /vesper.
#
#   build:   docker build -t localhost/jarvis:local .         (--build-arg JARVIS_LOCAL_VOICES=0 skips ~9 GB of voices)
#   run:     see compose.yaml
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

ARG TARGETARCH
ARG S6_OVERLAY_VERSION=3.2.3.0
ARG S6_OVERLAY_NOARCH_SHA256=b720f9d9340efc8bb07528b9743813c836e4b02f8693d90241f047998b4c53cf
ARG S6_OVERLAY_X86_64_SHA256=a93f02882c6ed46b21e7adb5c0add86154f01236c93cd82c7d682722e8840563
ARG S6_OVERLAY_AARCH64_SHA256=0952056ff913482163cc30e35b2e944b507ba1025d78f5becbb89367bf344581
ARG S6_OVERLAY_SYMLINKS_SHA256=a60dc5235de3ecbcf874b9c1f18d73263ab99b289b9329aa950e8729c4789f0e
# The self-hosted voices, CPU only: 1 = OmniVoice + Pocket TTS (~9 GB), pocket = Pocket TTS only (the published
# image), 0 = none (ElevenLabs / Edge / Piper). Pocket TTS is pinned by the OmniVoice lock either way, so every
# build has the same torch and packages and speaks a voice exactly the same.
ARG JARVIS_LOCAL_VOICES=1

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

# What Hermes's installer needs (it brings its own Python, Node, ffmpeg and ripgrep into the volume), the launcher
# (system Python + aiohttp + PyYAML), and the browser desktop (Chromium on Xvfb, shown through noVNC).
# The base image is pinned by digest; upgrading brings its own packages up to Debian's current security fixes.
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        ca-certificates curl git xz-utils libatomic1 procps tzdata openssl \
        python3 python3-aiohttp python3-yaml \
        chromium xvfb x11vnc novnc websockify openbox x11-utils xauth fonts-dejavu-core \
        ffmpeg libportaudio2 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay supervises the launcher and the desktop (same pinned release and checksums as Hermes's own image).
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) s6_arch="x86_64"; s6_arch_sha="${S6_OVERLAY_X86_64_SHA256}" ;; \
        arm64) s6_arch="aarch64"; s6_arch_sha="${S6_OVERLAY_AARCH64_SHA256}" ;; \
        *) echo "Unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"; \
    curl -fsSL --retry 3 -o /tmp/s6-noarch.tar.xz "${base}/s6-overlay-noarch.tar.xz"; \
    curl -fsSL --retry 3 -o /tmp/s6-symlinks.tar.xz "${base}/s6-overlay-symlinks-noarch.tar.xz"; \
    curl -fsSL --retry 3 -o /tmp/s6-arch.tar.xz "${base}/s6-overlay-${s6_arch}.tar.xz"; \
    printf '%s  %s\n' "${S6_OVERLAY_NOARCH_SHA256}" /tmp/s6-noarch.tar.xz "${s6_arch_sha}" /tmp/s6-arch.tar.xz \
        "${S6_OVERLAY_SYMLINKS_SHA256}" /tmp/s6-symlinks.tar.xz | sha256sum -c -; \
    for t in noarch arch symlinks; do tar -C / -Jxpf "/tmp/s6-$t.tar.xz"; done; \
    rm /tmp/s6-*.tar.xz

# The self-hosted voices (HANDOFF 31/32): their own Python 3.12 with CPU torch, every package pinned, the models baked in
# at fixed revisions so nothing downloads at runtime. Early in the file so these layers stay cached across deploys.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
COPY deployment/omnivoice-requirements.txt /opt/omnivoice/requirements.txt
COPY deployment/pocket-tts-requirements.txt /opt/pocket-tts/requirements.txt
RUN set -eu; case "$JARVIS_LOCAL_VOICES" in 1|pocket) ;; \
      *) echo "Skipping the self-hosted voices (JARVIS_LOCAL_VOICES=$JARVIS_LOCAL_VOICES)"; exit 0 ;; esac; \
    UV_PYTHON_INSTALL_DIR=/opt/omnivoice/python uv venv --python 3.12 /opt/omnivoice/venv; \
    if [ "$JARVIS_LOCAL_VOICES" = 1 ]; then \
      uv pip install --no-cache --python /opt/omnivoice/venv/bin/python \
        --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /opt/omnivoice/requirements.txt; \
      HF_HOME=/opt/omnivoice/hf /opt/omnivoice/venv/bin/python -c "from huggingface_hub import snapshot_download; \
snapshot_download('k2-fsa/OmniVoice', revision='c5fdb5ccb189668d56333f77ba2629f4cd7535f4')"; \
    fi; \
    uv pip install --no-cache --python /opt/omnivoice/venv/bin/python \
      --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /opt/pocket-tts/requirements.txt numpy soundfile pyyaml -c /opt/omnivoice/requirements.txt; \
    HF_HOME=/opt/pocket-tts/hf /opt/omnivoice/venv/bin/python -c "from huggingface_hub import hf_hub_download, snapshot_download; \
r = 'kyutai/pocket-tts-without-voice-cloning'; \
hf_hub_download(r, 'languages/english/model.safetensors', revision='e7205b6ee50e654a5ea19f0e9df2b0813b05e921'); \
hf_hub_download(r, 'languages/english/tokenizer.json', revision='00eac05ed3d16bdc3f6b5d598874019c34a89214'); \
snapshot_download(r, revision='4e1e0a3e611c51c0b4ed8174fc10f32a54644303', allow_patterns=['languages/english/embeddings/*'])"; \
    rm -rf /opt/omnivoice/hf/xet /opt/pocket-tts/hf/xet; chmod -R a+rX /opt/omnivoice /opt/pocket-tts

# Piper: the last-resort local voice for every persona (rhasspy/piper-voices v1.0.0), so the first spoken fallback
# isn't a download. Hermes's own Piper TTS reads them from here (vesper_voice.py sets tts.piper.voices_dir).
RUN set -eu; mkdir -p /opt/piper-voices; cd /opt/piper-voices; \
    base=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en; \
    for v in en_GB/alan/medium/en_GB-alan-medium en_US/ryan/medium/en_US-ryan-medium en_US/amy/medium/en_US-amy-medium; do \
      curl -fsSL --retry 3 -O "$base/$v.onnx"; curl -fsSL --retry 3 -O "$base/$v.onnx.json"; \
    done; chmod -R a+rX /opt/piper-voices

# Everything runs as hermes (uid/gid 10001, the same as Hermes's own image, so existing data volumes keep working).
RUN groupadd --gid 10001 hermes && useradd --uid 10001 --gid 10001 --home-dir /opt/data --no-create-home \
        --shell /bin/bash hermes && mkdir -p /opt/data && chown hermes:hermes /opt/data

# Xvfb runs as hermes and can't create its socket directory itself.
RUN mkdir -m 1777 -p /tmp/.X11-unix
# A bare visit to the browser-desktop port opens the desktop, not a file listing.
RUN printf '%s\n' '<!doctype html><meta charset="utf-8"><title>JARVIS browser desktop</title>' \
      '<meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=true&amp;resize=remote">' \
      '<a href="vnc.html?autoconnect=true&amp;resize=remote">Open the browser desktop</a>' > /usr/share/novnc/index.html

COPY image/s6-rc.d/ /etc/s6-overlay/s6-rc.d/
COPY image/cont-init.d/ /etc/cont-init.d/
COPY desktop/ /opt/jarvis/desktop/
COPY persona/ /opt/jarvis/persona/
COPY launcher/ /opt/jarvis/launcher/
COPY connector/ /opt/jarvis/connector/
# The voice pack: encrypted, unlocked at start by JARVIS_VOICE_KEY (image/cont-init.d/15-jarvis-voices).
COPY voices/ /opt/jarvis/voices/
RUN find /etc/s6-overlay/s6-rc.d -name run -exec chmod 0755 {} + \
    && chmod 0755 /etc/cont-init.d/* /opt/jarvis/desktop/start.sh \
    && find /opt/jarvis -type d -name tests -prune -exec rm -rf {} + \
    && chmod -R a+rX /opt/jarvis \
    && python3 -c "import sys; sys.path.insert(0, '/opt/jarvis/launcher'); import jarvis_launcher.server"

ENV HERMES_HOME=/opt/data HOME=/opt/data \
    PATH=/opt/data/.local/bin:/command:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/jarvis/launcher PYTHONUNBUFFERED=1 \
    S6_KEEP_ENV=1 S6_BEHAVIOUR_IF_STAGE2_FAILS=2 S6_SERVICES_GRACETIME=75000 S6_KILL_GRACETIME=5000

VOLUME ["/opt/data"]
WORKDIR /opt/data
EXPOSE 8642 6080
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8642/jarvis/health >/dev/null || exit 1
ENTRYPOINT ["/init"]
