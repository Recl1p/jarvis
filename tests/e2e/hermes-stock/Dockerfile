# A stock Hermes Agent, installed the way a JARVIS user would have it (Nous Research's official installer), for the
# "I already have Hermes" end-to-end tests. Its API server starts OFF, as on a fresh install; the connect script the
# tests run turns it on. A small "agent" loop stands in for the user's Hermes running the pasted prompt: it executes
# /shared/run.sh whenever the tests drop one there, and writes the output next to it.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
ARG HERMES_CHANNEL=stable
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git xz-utils libatomic1 procps openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/hermes --shell /bin/bash hermes
USER hermes
WORKDIR /home/hermes
ENV HOME=/home/hermes PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/install.sh \
    && bash /tmp/install.sh --non-interactive --skip-browser --skip-computer-use --channel "$HERMES_CHANNEL" \
    && rm /tmp/install.sh && hermes --version
COPY --chown=hermes:hermes entry.sh /home/hermes/entry.sh
ENTRYPOINT ["bash", "/home/hermes/entry.sh"]
