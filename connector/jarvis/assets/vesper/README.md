# JARVIS / VESPER dashboard

The dashboard implements the supplied `JARVIS UI improvement` Claude Design
handoff (`JARVIS Options.dc.html` and its `JARVIS.dc.html` component). The header's
layout button switches between **Floating Glass** and **Cinematic**, saved in this
browser as `vesperLayout`. The export's shared `support.js` matches the installed
runtime, so no runtime or backend upgrade is needed.

The collapsible sidebar contains vitals and real tool activity. Cinematic mode and
smaller screens also put agenda, radio, and connection checks in the sidebar.
Weather uses the existing geolocation endpoint; no location permission displays
“Location unavailable.” The orb responds to the live conversation state and voice
analyser. The dialogue dock retains authenticated Hermes streaming chat, mic/STT,
TTS, and the session model picker. Ctrl/Cmd+K opens searchable keyboard commands.

The operations board reads the canonical scheduled jobs, including paused and
completed jobs. Clicking a card opens that agent's real settings/results; scheduling
a new agent opens the existing creation form. Cards cannot manually advance a job's
execution state. The Internet Speed panel is the browser speed test (`cloudflare-speedtest.js`), which
measures download, upload and ping.

Frontend edits are served immediately. Refresh `/vesper` to load them. Responsive
layouts cover mobile, tablet, and desktop; reduced-motion preferences are honored.

## Agent control

Open **AGENT CONTROL · OPEN** in the Vitals sheet, or say "open agent control". The panel adapts
the agent creation and management workflow shown in the [OpenJarvis demo](https://github.com/open-jarvis/OpenJarvis/tree/main/assets)
to Hermes's scheduler. It uses Hermes tools, skills, providers, and stored jobs.

- Create an agent from a template, replace its bracketed placeholders, and set
  instructions, model, schedule, and optional toolsets/skills.
- Saving schedules the job. **Run now** queues it for the next scheduler tick
  and also resumes a paused job. **Pause schedule** does not interrupt a run.
- Edit settings, view the latest execution status, or inspect the last 20 saved
  Markdown outputs. Output is rendered as text; previews are capped at 256 KB.
- Deleting an agent also deletes its saved output through Hermes's existing API.
- Results stay local. Templates using email or calendar need those tools to be
  configured already. The form does not connect new services.

Schedules accept Hermes duration phrases (`every 6h`), five-field cron expressions
(`0 8 * * 1-5`), or ISO timestamps. Cron uses Hermes's configured timezone. One-shot
jobs follow Hermes's normal completion/retention behavior. Tool selection requests
toolsets; runtime credential checks still apply. Empty custom selection is rejected
because Hermes treats an empty list as its default tools.

`dashboard.dc.html` opens the panel via `vesper:agents` (optional event detail
`{id}` to select an agent or `{create: true}` to open the form). Mutations emit
`vesper:agents-changed` to refresh the board and status panels. `agents.js` implements an
isolated custom element so the existing DC component, animations, audio, and chat
continue working. `gateway/platforms/api_server_vesper.py` registers its routes
through the existing API adapter. Restart the gateway after backend changes, then
reload `/vesper`; subsequent changes to the HTML or JS are served fresh.

Authenticated routes added:

| Route | Purpose |
| --- | --- |
| `POST /vesper/agents` | Create with validated model, provider, toolsets, and skills |
| `PATCH /vesper/agents/{job_id}` | Edit the same fields in the canonical job store |
| `GET /vesper/agents/{job_id}/output?file=…` | Read bounded saved output |

The panel also uses the existing `/api/jobs`, `/api/model/options`, `/v1/skills`,
and `/v1/toolsets` endpoints. It follows the dashboard's existing loopback auth
arrangement. No API key is stored in browser storage by the panel.

Validation: `scripts/run_tests.sh tests/gateway/test_vesper_agents.py tests/gateway/test_api_server_jobs.py`.

Browser validation: `scripts/run_tests.sh tests/gateway/test_vesper_browser.py -m integration`.
It exercises layout persistence, mobile controls, real scheduler create/pause/edit,
board-to-agent navigation, output escaping, and authenticated chat/TTS transport
with test responses. It needs Playwright and Chromium and never calls a live model.

## Notes, local commands, and the setup guide

- **Notes**: the sidebar ✎ tab, backed by `/vesper/notes` (`api_server_vesper.py`, one JSON file per
  note under `HERMES_HOME/vesper-notes`). Dictation reuses `/vesper/stt`. Voice and typed note
  commands are `notes.*` entries in `LOCAL_COMMANDS`; the agent writes notes through the bundled
  `jarvis-notes` skill. Tests: `tests/gateway/test_vesper_notes.py`,
  `tests/gateway/test_vesper_stt_format.py`.
- **Local commands**: `LOCAL_COMMANDS` in `dashboard.dc.html` matches the utterance after
  `cleanUtterance` (fillers and typos), first as a whole and then clause by clause, before `run()`
  sends anything to the agent. `focusPanel(key)` then opens the panel where the current layout keeps
  it (a sidebar tab, or a pulse on the floating glass card).
- **Setup guide**: `tour.js` defines `<vesper-tour>`, served at `/vesper/tour.js`. Open it with the
  `vesper:tour` window event (`detail: {step?, reason?: 'manual'|'no-model'}`). It has five steps
  (`STEPS`): welcome, connect, model, around, done. The free route calls
  `/vesper/setup/openrouter-free*` and `/vesper/setup/status`, and sends `vesper:ui`
  `setup-complete` so the chat switches to the chosen model. It dispatches `vesper:tour-closed` and
  sets `localStorage.vesperTourDone`. The dashboard opens it once on a browser's first visit.
  Spotlight targets are `data-tour="…"` attributes in the dashboard template.
- **Personas**: `personas.js` sets `window.VESPER_PERSONAS` (`jarvis`, `ultron`, `cortana`) and
  `window.vesperPersonaLine()`. Each persona has its own CSS filter on the UI and its own lines.
- **Chats**: `sessions.js` defines `<vesper-sessions>`, opened by `vesper:sessions`. It asks the
  dashboard to change chat with `vesper:session-switch` (`{id, fresh?}`), and exposes
  `window.vesperSessions` (`messages`, `usage`, `create`).
- **Diagnostics**: `POST /vesper/doctor` (`api_server_setup.py`) runs `hermes doctor` (never `--fix`)
  and returns the redacted report, with a summary of its check markers. The Telemetry panel's
  RUN DIAGNOSTICS button calls it.
- **`vesper:ui`** window event: page-level actions requested by other components. `detail.action`
  is `model-picker`, `refresh-models`, `setup-complete` (with `model` and `provider`), or `panel`
  (with `detail.panel`).
