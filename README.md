# JARVIS

JARVIS is a dashboard, voice and browser desktop for **[Hermes Agent](https://hermes-agent.nousresearch.com)**,
Nous Research's open-source agent. Hermes is the brain (chat, memory, skills, tools, messaging, scheduled tasks);
JARVIS is how you talk to it. **Hermes is not part of JARVIS**: JARVIS either installs the official Hermes for you, with
your consent, or connects to the Hermes you already run.

> **Installing it:** [INSTALL.md](INSTALL.md), for TrueNAS, ZimaOS or any computer with Docker. In short: paste the
> app YAML (or `docker compose up -d`), open `http://<server>:8642`, create the password, and follow the setup screen.

## How it fits together

```text
                     ┌──────────────────── JARVIS container (this repo) ──────────────────────┐
browser ── :8642 ──► │ launcher (launcher/): the password first (auth.py), then                │
                     │   /jarvis/   setup screen: install Hermes, or connect yours; settings   │
                     │   /desktop/  browser desktop (desktop/): Chromium on Xvfb, noVNC, CDP   │
                     │   anything else ──► proxied to the Hermes JARVIS uses                   │
                     │ local voices: Pocket TTS (+ OmniVoice), the voice pack, Piper voices    │
                     └──────────────┬──────────────────────────────────────────────────────────┘
                                    │  managed: Hermes installed in the data volume, API on 127.0.0.1:8643
                                    │  external: your Hermes's API server, anywhere JARVIS can reach
                                    ▼
                     ┌──────────────────────── stock Hermes Agent ─────────────────────────────┐
                     │ API server (/health, /v1, /api/sessions, ...)                            │
                     │ + the "jarvis" connector plugin (connector/jarvis):                      │
                     │   /vesper dashboard, setup guide, voice, notes, music, PDF-to-Markdown   │
                     └──────────────────────────────────────────────────────────────────────────┘
```

- **`connector/jarvis/`** is a normal Hermes plugin. It attaches the dashboard to Hermes's own API server through
  `ctx.register_platform_handler("api_server", ...)`, keeps the `jarvis-notes` skill in the user's skills folder and
  turns PDFs sent on Telegram into Markdown (`pre_gateway_dispatch`). Everything JARVIS used to patch into a forked
  Hermes lives here now; Hermes itself is unmodified. It needs `plugins.isolation: in_process` (Hermes's default).
- **`launcher/`** is JARVIS's front door. Every request passes its login first (a password created on the first
  visit; a session cookie, Basic auth, or Hermes's API key). Before it knows a Hermes it serves the setup screen;
  afterwards it forwards everything outside `/jarvis/` and `/desktop/` to that Hermes, keeping the browser's `Host` so
  the dashboard stays same-origin, and vouches for each forwarded request (`X-JARVIS-Launcher`), without which the
  connector won't serve the dashboard or downloads.
- **Managed mode** ("Install Hermes for me"): after the user ticks the consent box, the launcher downloads Nous
  Research's installer (`https://hermes-agent.nousresearch.com/install.sh`), installs Hermes into the data volume
  (`/opt/data/hermes-agent`, `HERMES_HOME=/opt/data`), sets it up for JARVIS (API server, connector, browser desktop,
  optionally the JARVIS persona in `SOUL.md`) and starts it. Hermes supervises its own gateway as an s6 service, as in
  Hermes's own image. Once a day (setting on the setup screen) JARVIS installs new stable Hermes releases and rolls back
  to the exact previous commit if the dashboard doesn't come up healthy afterwards.
- **External mode** ("I already have Hermes"): JARVIS finds Hermes on this computer and the network, or takes an
  address and API key, or gives you a prompt to paste into your Hermes. That prompt has your Hermes run
  `/jarvis/connect.sh`, which installs the connector, turns on its API server and pairs back to JARVIS with a one-time
  code. **Test connection** (setup screen, and step 1 of the dashboard's GUIDE) checks every link in the chain.

## Troubleshooting

**Run `hermes` commands in the container as the `hermes` user.** Hermes runs as `hermes` (uid 10001); a root
`docker exec` writes root-owned files (such as `/opt/data/auth.json`) that the gateway can't read, which looks exactly
like "No Anthropic credentials found". Always:

```sh
docker compose exec --user hermes jarvis hermes model          # or: docker exec -u hermes jarvis hermes ...
```

To repair a root-owned file: `docker exec jarvis chown hermes:hermes /opt/data/auth.json`, then
`docker exec -u hermes jarvis hermes gateway restart`.

**The setup screen is always at `/jarvis/`**, also once JARVIS is set up: connection details, Test connection, updates,
the persona, and switching to another Hermes.

**After enabling the connector in a running Hermes, restart its gateway.** Hermes hot-loads the plugin, but a running
API server can't take new routes: `hermes gateway restart` (the connect script does this for you).

<!-- bundle:exclude-start -->
**A cron job with `enabled: false`, `state: "completed"`, and `repeat: {times: N, completed: N}`
finished normally** — it ran all of its scheduled repeats and stopped on purpose. This is not a
stuck or broken job and does not need "resuming" (unlike `state: "paused"`, which does). Example:
job `0a91def21adf` ("Message Moe reminder"), `repeat: {times: 7, completed: 7}`.
<!-- bundle:exclude-end -->

**The dashboard's custom "dc" component framework only ever passes one argument to
`componentDidUpdate`** (`dashboard.dc.html`), never a second `previousState` argument like
React's `componentDidUpdate(prevProps, prevState)`. Code that assumes a second argument exists
will throw on every re-render (caught internally and logged via `console.error`, invisible to
users but noisy in devtools). Track previous state yourself with an instance property instead
(e.g. `this._lastTranscript`) if you need to diff against it.

## Build and start

The published image is `ghcr.io/recl1p/jarvis` (x86-64; tags `2`, `2.1`, `2.1.0`, `latest`): see
[INSTALL.md](INSTALL.md). To build it from this repository instead (Docker with Compose v2.24 or newer):

```sh
cp .env.example .env      # optional: voice key, port, time zone, auto-update hour
docker compose -f compose.yaml -f compose.build.yaml up -d --build      # or ./setup.sh --build
```

Open `http://localhost:8642`, create the password, then **Install Hermes for me** or **I already have Hermes**; after
that the dashboard opens at `/vesper` with its GUIDE, which connects a model (no terminal needed).

`JARVIS_LOCAL_VOICES` picks the self-hosted voices: `pocket` (the default, as published) builds in Pocket TTS, pinned by
the OmniVoice lock so every build speaks a voice identically; `1` adds OmniVoice (about 9 GB more); `0` has only the
ElevenLabs, Microsoft Edge and Piper voices. The voice pack (`voices/`) is encrypted and unlocked at start by
`JARVIS_VOICE_KEY`; `scripts/friend-files.sh` writes the TrueNAS and ZimaOS YAML with the key filled in.

To publish a release: `scripts/publish-image.sh 2.1.0` builds the image and pushes `2.1.0`, `2.1`, `2` and `latest` to
GHCR (or push a `v2.1.0` tag and the GitHub workflow does it).

### Tests

Each runs in throwaway containers, so nothing needs installing (prefix `DOCKER="sudo -n docker"` where docker needs sudo):

```sh
tests/unit.sh              # launcher unit tests (Python container)
tests/connector.sh         # connector tests against a stock Hermes from the official installer
tests/e2e/run.sh           # Playwright, end to end: setup screen, a real Hermes install, the dashboard, the API,
                           # settings, and "I already have Hermes" against a stock Hermes (about 15 minutes)
tests/e2e/run.sh --live http://host:8642 [--chat]   # smoke test an instance in use (read-only)
```

The end-to-end chat tests talk to `tests/e2e/mock_llm.py`, a small OpenAI- and Ollama-compatible server with
deterministic replies, connected through the dashboard guide's own Local route. Reports land in
`tests/e2e/playwright/report/<phase>/`; a failure keeps its trace, screenshot, video and every container's log in
`tests/e2e/playwright/results/`.

## Dashboard controls

**AGENT CONTROL · OPEN** (in the Vitals sheet, or say "open agent control")
creates and manages Hermes's scheduled agents: templates, schedules, run now,
pause, and saved results. Results stay local. The panel is described in
`connector/jarvis/assets/vesper/README.md`.

The **BROWSER** control (or "open the browser") opens the browser desktop. Log
into sites there and then ask Hermes to continue. Browser profile data persists
in the application data volumes. Do not paste passwords or MFA codes into chat,
and avoid running competing browser tasks at the same time.

**RUN DIAGNOSTICS**, in the Telemetry panel (or say "run diagnostics"), runs
`hermes doctor` on the server. It is read-only and redacts secrets.

The clock switches between 12- and 24-hour time: click it (desktop) or say
"use 12 hour time". Agenda times follow it.

**SKIP VOICE**, or **Escape**, cancels pending or playing Hermes narration while
preserving the answer text. **INTERNET SPEED** measures the browser's download,
upload and latency; **Cancel test** stops an active measurement.

The **GUIDE** button (**?** on phones) opens the setup guide (`assets/vesper/tour.js`). Nothing in
it needs a terminal. It has seven steps:
- **Welcome.**
- **Connect a model**, four ways: **Free** (paste an OpenRouter key and JARVIS picks a free model that can use
  tools; daily request limits apply), **Sign in** with a plan you pay for (Claude, ChatGPT, Grok, Nous Portal,
  MiniMax), an **API key**, or a **Local** server (Ollama, LM Studio).
- **The voice** (optional). **On this server**: Pocket TTS speaks out of the box with a stock voice; with a free
  Hugging Face account and a read token the guide downloads Kyutai's voice-cloning model, so it speaks in the
  persona's own voice. **ElevenLabs**: paste a key, then pick one of your account's voices for each persona.
- **Telegram** (optional). Make a bot with @BotFather, paste its token, press Start in the bot's chat, and pick
  yourself. JARVIS saves the bot for you alone, sets your chat as its home channel, and starts it without a restart.
- **Pick the model**, **Getting around** and **Done.**

It opens by itself once on a browser's first visit, starting at the connect step when no provider is connected yet.
Reopen it any time from GUIDE, the ⌘K menu, or by saying "open the guide", "set up voices" or "set up telegram";
SET UP VOICES under VOICE in the Vitals panel opens the voice step. The written version of the model step is
[docs/CONNECTING-MODELS.md](docs/CONNECTING-MODELS.md).

### Local commands (voice or typed)

Some requests are about the dashboard itself, and the agent can't act on the page. These are
handled in the browser, instantly, without a model call. The matching panel opens wherever the
current layout keeps it: a sidebar tab, or, on a wide Floating Glass layout, a highlight pulse on
the floating card. Works the same from the mic, the composer, and the ⌘K menu.

| Say or type | What happens |
| --- | --- |
| "take a note", "new note", "dictate a note" | Opens Notes with a new note. From the mic it starts dictating right away; typed, it focuses the editor. |
| "take a note: buy milk", "note to self, call the bank", "make a note of …" | Saves that text as a new note and shows it. |
| "add eggs to my grocery note" | Appends the item to that note and shows it. |
| "open my grocery note", "read my grocery note" | Opens that note; "read" also speaks it. |
| "open my notes" | Opens Notes. |
| "play some music", "play my music", "play the radio", "play Groove Salad", "play Sirens" | Opens Audio and plays a station or an uploaded track by name ("my music" uses your uploaded library). |
| "pause the music", "next song" | Controls playback. |
| "stop talking" | Cuts JARVIS off mid-sentence. With nothing being said, it pauses the music. |
| "run a speed test", "how fast is my internet?" | Opens Internet Speed and starts a measurement. |
| "show my agenda", "open vitals", "show telemetry", "open the operations board" | Opens that panel. |
| "show the weather" | Shows the weather. |
| "open agent control" | Opens Agent Control. |
| "open the browser" | Opens the browser desktop. |
| "run diagnostics" | Runs `hermes doctor` on the server and shows the result (see Dashboard controls). |
| "new chat", "show my chats" | Starts a new chat, or opens the Chats list. |
| "switch to cinematic" | Switches to the Cinematic layout. |
| "hide the chat" | Hides the dialogue; "show the chat" brings it back. |
| "use 12 hour time", "show fahrenheit" | Switches the clock format, or the temperature units. |
| "switch to Ultron", "switch to Cortana", "switch to JARVIS" | Changes persona. |
| "switch model", "open the guide", "how do I connect a cloud model?" | Opens the model picker or the setup guide. |
| "set up voices", "connect elevenlabs", "set up telegram" | Opens the setup guide at its voice or Telegram step. |

Matching is forgiving. Fillers and politeness ("please", "on jarvis", "real quick") are ignored,
and small typos ("play my muisic") still match. A command can also trail another phrase ("run
diagnostics, something's off"). Questions about the weather, the agenda or the internet bring up
the matching panel too: a flash on desktop, a button under the answer on phones. Anything else
goes to the agent as usual, including questions that only mention these things ("what's a good
way to take notes?", "take notes on this video", "what's on my agenda today?"). Messages with
attachments always go to the agent. The table of patterns is `LOCAL_COMMANDS` in
`dashboard.dc.html`.

### Personas

JARVIS (the default, dry British wit), **Ultron** (crimson theme, deep voice) and **Cortana**
(violet theme, warm voice) are the three personas. Pick one in the Vitals sheet (PERSONA), by
voice ("switch to Ultron"), or from the ⌘K menu. **ADDRESS ME AS** sets nothing, sir, ma'am, boss
or a name. These are homage personas written in original wording, not the trademarked characters.
The ElevenLabs voice ids in `assets/vesper/personas.js` are the author's own voices, which other accounts don't
have: pick your own voice for each persona in the guide's voice step (saved as `tts.elevenlabs.persona_voices`).
Without one, ElevenLabs uses the configured voice, and when ElevenLabs can't speak, Pocket TTS on the server does.
The persona definitions are in `assets/vesper/personas.js`.

### Chats

The **CHATS** button in the dialogue header (or "show my chats", "new chat") lists your chats with
a meter showing how full each one's context window is. From there you can start a new chat,
switch, rename or delete. Past about 80% of the context window, JARVIS suggests starting a new
chat. The list is in `assets/vesper/sessions.js`.

### Slow replies

When a reply is slow, JARVIS says a short acknowledgement, worded for the tool it is using, and
checks in at 20 s, 60 s, 150 s and 300 s. The real answer replaces that line.

### Notes

The sidebar's **✎ Notes** tab is a voice-first notepad. **+ NEW NOTE** or **DICTATE** starts a note.
**DICTATE** records the mic and, when you tap **STOP**, transcribes the recording with the same
server-side STT as the main mic (local faster-whisper by default) and appends the text to the end
of the note. The text stays fully editable. Notes save automatically about a second after you stop
typing, when you leave the editor, and after each dictation. An empty new note is never stored.
Search filters by title and text. A single dictation can run for up to 8 minutes, which keeps it
under the gateway's 10MB request cap.

Notes also work by voice or typing: "take a note: …", "add eggs to my grocery note", "open my
grocery note" (see [Local commands](#local-commands-voice-or-typed)). The agent can write notes
too, through the bundled `jarvis-notes` skill. If you say something like "let's pick up where we
left off on my RTX notes", that note's text is sent to the agent, and so to your model provider.

- **Storage**: one JSON file per note (`{id, title, body, created_at, updated_at}`, UTC
  timestamps) under `get_hermes_home()/vesper-notes/` (`/opt/data/vesper-notes/` on the data
  volume), written atomically with mode 0600. Because they live on the server, notes follow you
  to every device that opens the dashboard. They're plain files, so the agent's file tools can
  read them if you point it there.
- **API** (all require the dashboard's `Authorization: Bearer` key): `GET/POST /vesper/notes`,
  `GET/PATCH/DELETE /vesper/notes/{id}`. Ids are 32 hex characters, validated before any
  filesystem access. Limits: title 200 characters (whitespace collapsed), body 200,000 characters.
- Two devices editing the same note at the same moment resolve last-write-wins.

### Phones and tablets

At ≤700px wide, the sidebar becomes a row of panel icons under the top bar, and each panel opens
as a full-height sheet (tap the active icon, ✕, or outside it to close). BROWSER and the layout
switch move into the **☰** menu (⌘K). Landscape and short screens fit, the command palette
scrolls, inputs use 16px text so iOS does not zoom, tap targets are larger, and the weather shows
in the Vitals sheet. The composer puts the text field on its own row
above 📎 / SPEAK / SEND. The mic and dictation record in whatever audio format the browser
supports (`audio/mp4` on iPhone Safari), and `/vesper/stt` names the upload after its
Content-Type so the STT pipeline accepts it. To reach the dashboard from a phone, see INSTALL.md's
Security section: don't bind the port to your LAN. Browsers only allow the microphone on HTTPS
or `localhost`, so voice input and dictation on a phone need a secure URL. `tailscale serve`
provides one; a plain `http://<LAN-IP>` address does not.

### Custom music library

The AUDIO panel's **MY MUSIC / LIVE RADIO** toggle switches between the built-in SomaFM
internet-radio stations and a personal uploaded library, sharing the same play/pause/prev/next
transport either way. The toggle and library controls appear in the sidebar Audio tab in both
layouts, and additionally in the floating glass-card panel when that layout is active.

- **Upload**: "+ UPLOAD" file picker (`accept="audio/*"`, multiple files at once). 100MB per
  file, formats mp3/wav/ogg/m4a/flac/aac/opus only — enforced both client-side (fast feedback)
  and server-side (the enforcement that actually matters, since a client can lie about file
  type).
- **Rename / delete**: per-track controls in the library list, using the dashboard's existing
  prompt/confirm pattern for destructive actions.
- **Storage**: uploaded files live under `get_hermes_home()/vesper-music` (i.e.
  `/opt/data/vesper-music/` on the persistent volume), named
  `{32-hex-uuid}__{percent-encoded-title}.{ext}`. There is no separate metadata database —
  listing, upload-time ordering, and title all derive from the filename plus `os.stat()`.
- **API**: `GET/POST /vesper/music/tracks` (list / upload, both require the dashboard's
  `Authorization: Bearer` key) and `GET/PATCH/DELETE /vesper/music/tracks/{track_id}`
  (stream / rename / delete). The streaming `GET` is intentionally unauthenticated — a bare
  `<audio src>` can't carry an `Authorization` header — and stays safe because the track id is
  validated against a strict 32-hex-character pattern before any filesystem access, the same
  trust boundary the existing `/vesper/radio` proxy already relies on.
- **Request-size cap**: the gateway's shared `body_limit_middleware` normally rejects any
  POST/PUT/PATCH body over `MAX_REQUEST_BYTES` (10MB) before any handler runs. Music uploads
  need up to 100MB, so requests whose path ends in `/vesper/music/tracks` get a
  `VESPER_MUSIC_UPLOAD_MAX_BYTES` ceiling instead (100MB plus a small framing margin). This is a
  hardcoded constant in `api_server.py`, **not** an environment variable — there is no way to
  reconfigure the cap without editing that file.

### Chat attachments

The composer's 📎 button lets you attach up to 4 files to a single chat message, so JARVIS can
read and reason about their contents in that conversation — analogous to Claude.ai's file
upload in chat. Attachments are inlined directly into that turn's message; nothing is written to
persistent server storage, so there is no attachment library, id scheme, or cleanup job to reason
about (unlike the music feature above).

- **Supported files**, by extension, client- and server-validated independently (server-side is
  authoritative, since a client can lie about file type):
  - **Images** — png/jpg/jpeg/gif/webp, up to 8MB each. Read client-side and inlined as an
    OpenAI-style `image_url` data-URL content part; the gateway already accepted this shape for
    any API caller before this feature existed (see `_normalize_multimodal_content` in
    `api_server.py`), so no backend change was needed for images beyond the request-size cap
    below.
  - **Plain text / code** — txt/md/csv/tsv/json/yaml/xml/html/css and most common source-code
    extensions, up to 2MB each. Read client-side (`FileReader.readAsText`), truncated to 500,000
    characters (with a `[…truncated…]` marker) and inlined as a plain `text` content part — split
    across multiple ≤64KB text parts rather than one oversized part (see the request-size-caps
    paragraph below for why).
  - **Documents** — PDF, Word (.doc/.docx), PowerPoint (.ppt/.pptx and OpenXML variants), Excel
    (.xls/.xlsx and variants), OpenDocument (.odt/.ods/.odp), RTF, EPUB, Jupyter notebooks
    (.ipynb) — PDFs up to 200MB, other documents up to Hermes's own 50MB limit. These need a real
    document-to-text converter, so the raw file bytes are POSTed to `/vesper/chat/extract-attachment`,
    which converts them to text using the same extraction pipeline the `read_file` tool uses (Hermes's
    `tools/read_extract.py`, backed by its `firecrawl-anydoc` package) and returns the extracted text,
    which the dashboard then inlines the same way as a text attachment. A PDF with some
    scanned/image-only pages keeps the real text from every other page: Hermes's converter answers such
    a PDF with only a `[NEEDS OCR: ...]` note, and the connector then adds the per-page `pdftotext`
    output (poppler-utils) under that note, a page without a text layer shown as
    `_(no text layer on this page)_`. A PDF over Hermes's 50MB limit goes the same per-page
    `pdftotext` way.
  - **PDFs are always converted to Markdown first** (`connector/jarvis/pdf_markdown.py`), before JARVIS
    analyzes them. The extraction endpoint returns a Markdown document (`# <filename>` title,
    then anydoc's Markdown conversion, with the scanned-page handling above) and saves the full,
    untruncated conversion as a `.md` file under
    `/opt/data/attachments/pdf-markdown/<32-hex>/<name>.md` (pruned after 30 days). The chat
    message tells JARVIS it is reading the Markdown conversion and gives the `.md` path, so if the
    inline copy was truncated it can `read_file` the rest. If anydoc can't convert the PDF at all
    (not installed, or it rejects a file poppler can still read), per-page `pdftotext` output is
    used instead with one `## Page N` heading per page; only a PDF with no readable text anywhere
    fails. The composer chip shows 📝 for a converted PDF.
  - **PDFs sent over messaging platforms** (Telegram etc.) get the same treatment: before the turn,
    the cached PDF is converted to a sibling `.md` in the document cache, and the context note inlines
    the first 60,000 characters of Markdown plus both paths, replacing the old "extract the text
    yourself" note. If conversion fails, the old note is used.
  - Native per-provider document blocks (e.g. Anthropic's `document` content type, for true
    vision-over-PDF-pages) are **not** implemented — every provider gets the same
    extracted-text treatment uniformly. See `HANDOFF.md` for why this was the deliberate choice
    over a provider-specific integration.
- **Server-side storage**: unlike music uploads, uploaded attachment bytes are never written to disk
  on the gateway. Text/code and image attachments never leave the browser except as part of the
  chat message itself; document attachments are read into memory, converted to text, and
  discarded. The one thing kept is the Markdown conversion of each PDF (see above); other document
  types return only their extracted text.
- **API**: `POST /vesper/chat/extract-attachment` (document extraction only — requires the
  dashboard's `Authorization: Bearer` key, filename passed via the `X-Attachment-Filename`
  header, raw file bytes as the body, same shape as the music upload endpoint). Responds with
  `{filename, text, truncated}`; for a PDF it also adds `format: "markdown"` and `markdown_path`
  (`null` if the `.md` couldn't be written). Images and text files never call a dedicated
  endpoint — they ride along as extra content parts in the existing
  `POST /api/sessions/{id}/chat` / `.../chat/stream` body.
- **Request-size caps**: `/chat` and `/chat/stream` get a `CHAT_MESSAGE_MAX_BYTES` ceiling
  (48MB) in place of the default 10MB, to admit inline base64 image attachments (base64 is ~33%
  larger than raw bytes); `/vesper/chat/extract-attachment` gets its own
  `CHAT_ATTACHMENT_EXTRACT_MAX_BYTES` ceiling (200MB), and music uploads 100MB. They are constants
  in `connector/jarvis/wiring.py`, **not** environment variables. Hermes's own body limit middleware
  refuses anything over 10MB, so the connector's outermost middleware (`jarvis_middleware`) calls
  exactly those routes directly, with a request cloned to the route's own `client_max_size`, when a
  body is bigger than that; smaller requests take Hermes's normal middleware chain. The extracted *text*
  returned to the dashboard is separately capped at 500,000 characters regardless of the source
  document's size (`connector/jarvis/vesper.py`'s `_ATTACHMENT_EXTRACT_TEXT_CAP`). That 500,000-character text is then split into multiple ≤64KB text content
  parts by the dashboard rather than sent as one oversized part: `_normalize_multimodal_content`
  independently caps *each* text part at `MAX_NORMALIZED_TEXT_LENGTH` (64KB) regardless of source,
  so a single giant part would just get silently re-clipped back down to 64KB after already
  surviving the extraction cap above — this bit a real large-textbook attachment in practice (its
  front matter/table of contents alone exceeded 64KB before reaching the requested chapter
  content), see `HANDOFF.md` item 10.

## Data and operation

Everything lives in the data volume mounted at `/opt/data` (the `jarvis-data` volume, or `JARVIS_DATA` in `.env`):

- Hermes's home (`HERMES_HOME`): `config.yaml`, `.env`, `auth.json`, memories, skills, sessions, cron jobs, notes,
  music, the connector plugin in `plugins/jarvis`;
- in managed mode, Hermes itself: `hermes-agent/` (the checkout) and `tools/` (its Python, Node, ffmpeg, ripgrep);
- JARVIS's own state in `jarvis/`: `state.json` (which Hermes, consent, update settings and history), `install.log`,
  `updates.log`, and the browser desktop's profile (`jarvis/browser`, or `openjarvis/browser` on older volumes).

Replacing the JARVIS container or image keeps all of it. Back the volume up (on a NAS: snapshot its dataset).

```sh
docker compose logs --tail=100 jarvis
docker compose exec --user hermes jarvis hermes gateway restart
docker compose down               # stops JARVIS, keeps the volume
```

Never `docker compose down -v` unless you mean to delete JARVIS and Hermes's data.

<!-- bundle:exclude-start -->
## History

Up to version 1 (the image `localhost/jarvis-unified:local`), JARVIS was a fork of Hermes 0.21 with the dashboard
patched into its API server, built on an OpenJarvis base image. Version 2 (2026-10-23) moved every JARVIS change into
the connector plugin, dropped the fork and the OpenJarvis runtime, and added the launcher. `HANDOFF.md` entry 34 has the
details, including how the NAS install moves over.
<!-- bundle:exclude-end -->
