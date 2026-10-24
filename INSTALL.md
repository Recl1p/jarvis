# Installing JARVIS

JARVIS is a dashboard, voice and browser desktop for [Hermes Agent](https://hermes-agent.nousresearch.com), Nous
Research's open-source AI agent. It does not include Hermes: on first start it either **installs the official Hermes
for you** (only after you agree) or **connects to the Hermes you already have**.

You need a server or computer with Docker: TrueNAS SCALE 24.10 or newer, ZimaOS (or CasaOS), or any Linux machine with
Docker. The image is for x86-64 (Intel or AMD) processors. Plan for about 5 GB of disk (the JARVIS image plus
Hermes) and 4 GB of memory free for JARVIS.

## Contents

- [TrueNAS SCALE](#truenas-scale)
- [ZimaOS or CasaOS](#zimaos-or-casaos)
- [Any computer with Docker](#any-computer-with-docker)
- [First visit: the password](#first-visit-the-password)
- [Install Hermes, or connect yours](#install-hermes-or-connect-yours)
- [Connect a model](#connect-a-model)
- [Voices](#voices)
- [Phones, the microphone and HTTPS](#phones-the-microphone-and-https)
- [Security](#security)
- [Updates](#updates)
- [Your data and backups](#your-data-and-backups)
- [Settings](#settings)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

## TrueNAS SCALE

1. Open **Apps**, then **Discover Apps**, then the three-dot menu at the top right, then **Install via YAML**.
2. Name the app `jarvis`.
3. Paste the whole of the `truenas.yaml` you were given (or [install/truenas.yaml](install/truenas.yaml)). Change
   `TZ` to your time zone if it isn't `America/New_York`.
4. **Save**. TrueNAS downloads the image (about 3 GB, a few minutes) and starts JARVIS.
5. Open `http://<your TrueNAS address>:8642` and go on with [the first visit](#first-visit-the-password).

Everything JARVIS keeps lives in the app's `jarvis-data` volume. To keep it on a dataset you choose (easier to
snapshot and back up), create a dataset first (for example `tank/apps/jarvis`), then replace the line
`- jarvis-data:/opt/data` with `- /mnt/tank/apps/jarvis:/opt/data` before saving.

## ZimaOS or CasaOS

1. Open the **App Store**, press the **+** at the top right, then **Install a customized app**.
2. Press **Import**, paste the whole of the `zimaos.yaml` you were given (or [install/zimaos.yaml](install/zimaos.yaml)),
   and press **Submit**. Change `TZ` to your time zone if needed.
3. Press **Install**. ZimaOS downloads the image (about 3 GB) and starts JARVIS.
4. Open JARVIS from its tile, or `http://<your Zima address>:8642`, and go on with
   [the first visit](#first-visit-the-password).

Everything JARVIS keeps is in `/DATA/AppData/jarvis/data`.

## Any computer with Docker

Docker Engine or Docker Desktop with Compose v2. In a folder with this repository's `compose.yaml` and `.env.example`
(or the files you were given):

```sh
cp .env.example .env          # then put the voice key you were given after JARVIS_VOICE_KEY=
docker compose up -d          # or ./setup.sh, which does both and waits until JARVIS answers
```

Open `http://localhost:8642` (or `http://<that computer>:8642` from another device).

## First visit: the password

The first page JARVIS shows asks you to **create a password**. It protects everything: the dashboard, the setup screen,
the browser desktop and the agent's API. Create it right after installing, because until a password exists, whoever
opens JARVIS first could set one. Each browser stays logged in for 30 days; **Log out** is at the bottom of the setup
screen (`/jarvis/`), and its settings can change the password.

Forgot it? Stop JARVIS, delete `jarvis/auth.json` in its data folder, and start it again: the next visit creates a new
password. Nothing else is lost. To set the password from the app's YAML instead, add `JARVIS_PASSWORD: "..."` under
`environment:`.

## Install Hermes, or connect yours

### Install Hermes for me (recommended)

The setup screen lists exactly what happens, and nothing is downloaded until you tick **I agree to download and install
Hermes Agent** and press **Download and install**:

1. JARVIS downloads Nous Research's official installer, `https://hermes-agent.nousresearch.com/install.sh` (its SHA-256
   is shown in the log and saved), and runs it inside the container on the **stable** release channel.
2. Hermes goes into the data volume (`/opt/data/hermes-agent`, with its own Python, Node and tools in `/opt/data/tools`),
   so updating or replacing the JARVIS container keeps it.
3. JARVIS sets Hermes up for itself: Hermes's API server on `127.0.0.1:8643` inside the container (JARVIS on 8642 is the
   only way in), the JARVIS connector plugin, Hermes's browser tools pointed at the browser desktop and, if you leave
   the box ticked, the JARVIS personality (`SOUL.md`; a previous one is kept as `SOUL.md.before-jarvis`).
4. It starts Hermes and opens the dashboard.

This takes a few minutes. The log on screen shows every step; if one fails, **Try again** keeps what already worked.

### I already have Hermes

JARVIS needs Hermes 0.22 or newer with its API server on, plus the JARVIS connector plugin. Three ways to connect:

- **Let your Hermes connect itself (easiest).** Copy the prompt from the setup screen into a chat with your Hermes. It
  downloads JARVIS's connect script, summarises it for you, and runs it with a one-time pairing code (valid for an
  hour). The script installs the connector into `~/.hermes/plugins/jarvis`, turns on the API server (on all network
  interfaces, so the JARVIS container can reach it), creates a key if there is none, sends the address and key to JARVIS
  and restarts the Hermes gateway. JARVIS then connects by itself; the key never goes through the chat. Prefer a
  terminal? The setup screen shows the same command.
- **Find my Hermes.** JARVIS looks on its own server, on the computer your browser is on, and on that computer's network
  (add more in `JARVIS_SCAN_SUBNETS`). It only finds a Hermes whose API server is already on and reachable.
- **Enter it.** The Hermes API server's address (for example `http://192.168.1.20:8642`) and its `API_SERVER_KEY` from
  `~/.hermes/.env`.

**Test connection** checks, in order: it answers, it is Hermes, it is new enough, the key works, the connector is
installed and current, plugins run in-process, and the dashboard loads. Each failed check says what to do.

With your own Hermes, the browser desktop and the built-in voices stay in the JARVIS container, so your Hermes keeps its
own browser settings and the dashboard speaks with ElevenLabs, Edge or Piper voices. JARVIS doesn't update your Hermes:
`hermes update` does.

## Connect a model

The dashboard opens its **GUIDE** when no model is connected. Step 1 shows the Hermes connection (with **Test
connection**); step 2 connects a model without a terminal: a free OpenRouter model, signing in with a Claude or ChatGPT
plan, an API key, or a local model server. See [docs/CONNECTING-MODELS.md](docs/CONNECTING-MODELS.md) for the details.

## Voices

The image has Pocket TTS, a voice engine that runs on your server's processor, with **JARVIS's, Ultron's and Cortana's
own voices built in**. They are locked: the `JARVIS_VOICE_KEY` in the YAML you were given unlocks them when JARVIS
starts (the container log says `voice pack unlocked: 3 voices`). Without the key, or with a wrong one, JARVIS uses its
original voices. Keep the key to yourself: the YAML you were given is the only place it is written down.

The **VOICE** control in the dashboard's Vitals panel switches engines. ElevenLabs (an online service with your own
account) is in the GUIDE's Voice step. Pocket TTS needs a reasonably recent processor to keep up; on a slow one the
first words of each reply take a moment longer.

## Phones, the microphone and HTTPS

Browsers only allow the microphone (talking to JARVIS) and location on HTTPS pages or on `localhost`. Over plain
`http://<server>:8642` from another device, everything else works and the mic button explains why it's off. The easy
way to get HTTPS, at home and away, is [Tailscale](https://tailscale.com): install the Tailscale app on the server
(TrueNAS and ZimaOS both have one) and your phone, then on the server run `tailscale serve --bg 8642`, and open the
`https://<server>.<your-tailnet>.ts.net` address it prints. Never use `tailscale funnel`: that publishes JARVIS to the
whole internet.

## Security

- **Everything is behind the password**: the setup screen, the dashboard, the browser desktop (`/desktop/`) and
  Hermes's API. Apps that talk to the Hermes API through JARVIS use Hermes's API key instead
  (`Authorization: Bearer ...`). Wrong passwords from one address are slowed down, then locked out for a while.
- **Only port 8642** is published. Hermes's own API server and the browser desktop listen inside the container only.
- **Don't expose JARVIS to the internet** (no port forwarding on your router). Use Tailscale to reach it from outside.
- **Plain http on your network** means someone who can watch your home network's traffic could read it. Use the
  Tailscale address when that matters to you.
- **The agent is powerful.** It runs commands and browses the web as you inside its container, and uses your model
  provider's credits. Its browser keeps the logins you make on the browser desktop.
- **"I already have Hermes"**: the connect script turns your Hermes's API server on for your whole network. It needs its
  key for everything, and opening its dashboard directly is refused, but firewall it to the JARVIS server if others
  share your network.
- Keep backups of the data folder private: they hold API keys, logins and the browser profile.

## Updates

- **Hermes (when JARVIS installed it).** The setup screen's Updates card: automatic once a day at the hour you pick
  (default 04:00), or **Check now** / **Update now**. An update stops Hermes, runs `hermes update` on the stable channel,
  starts it and waits until the dashboard's connector answers. If it doesn't, JARVIS reinstalls the exact version that
  worked before and says so on the setup screen. History is in `/opt/data/jarvis/updates.log`.
- **JARVIS itself.** The YAML uses the image `ghcr.io/recl1p/jarvis:2`, which always points at the newest 2.x release.
  On TrueNAS: **Apps**, select JARVIS, and update it when TrueNAS offers an update (or Edit, then Save, to pull the
  image again). On ZimaOS: update the app from its settings, or install it again with the same YAML. With Docker:
  `docker compose pull && docker compose up -d`. Your data stays either way.

## Your data and backups

Everything is in the data folder (`/opt/data` inside the container): Hermes and its home (settings, credentials,
memories, skills, chats, scheduled tasks, notes, music), JARVIS's state and login in `jarvis/`, and the browser profile.
Back it up while JARVIS is stopped, or snapshot its dataset on TrueNAS. With Docker:

```sh
docker compose stop
docker run --rm -v jarvis_jarvis-data:/data -v "$PWD":/backup debian:trixie-slim tar -czf /backup/jarvis-data.tgz -C /data .
docker compose start
```

## Settings

Environment variables (in the app's YAML under `environment:`, or `.env` for Docker; see [.env.example](.env.example)):
`TZ`, `JARVIS_VOICE_KEY`, `JARVIS_PASSWORD`, `JARVIS_AUTO_UPDATE` / `JARVIS_AUTO_UPDATE_HOUR` (the first-run defaults;
the setup screen's choice wins afterwards), `JARVIS_SCAN_SUBNETS`, `JARVIS_HERMES_URLS` (addresses to try first),
`JARVIS_PUBLIC_URL` (the address your own Hermes should use to reach JARVIS, if it differs from the one in your
browser), and `JARVIS_AUTH=off` (no login, only for a JARVIS reachable solely through something that already
authenticates). Restart the app after changing one.

## Uninstalling

TrueNAS: **Apps**, select JARVIS, **Delete** (tick removing its volumes to delete the data too). ZimaOS: the app's
menu, **Uninstall**, then delete `/DATA/AppData/jarvis` to remove the data. Docker: `docker compose down` keeps the
data; `docker compose down -v` also **deletes** it, Hermes and everything it remembers included. For your own Hermes,
`hermes plugins remove jarvis` removes the connector.

## Troubleshooting

- **The page doesn't open.** Give it a minute after the first start. Check the app's logs (TrueNAS: Apps, JARVIS, Logs;
  ZimaOS: the app's settings, Logs; Docker: `docker compose logs --tail=50 jarvis`). Port 8642 must not be used by
  another app.
- **The setup screen says the install failed.** The log shows the step. Common causes: no internet access from the
  container, less than 3 GB free, or the Hermes site being briefly down. **Try again** resumes.
- **The voices are the original ones.** The log line `[jarvis-voices]` says why: no key, or a key that doesn't match
  this image. Check `JARVIS_VOICE_KEY` against what you were given, then restart the app.
- **Test connection: "Hermes answers" fails (your own Hermes).** Is its gateway running (`hermes gateway status`)? Is
  its API server on and listening on `0.0.0.0`? Is the port open? From the container, `host.docker.internal` is the
  Docker host.
- **"The JARVIS connector is installed" fails right after the connect script.** The gateway restarts a few seconds
  after the script ends; test again. If it still fails, `hermes gateway restart` loads it.
- **The dashboard says "No model".** Open the GUIDE (? on phones) and connect one.
- **A Hermes command in the container says "Permission denied".** Run it as the `hermes` user:
  `docker exec -it -u hermes jarvis hermes <command>`.
