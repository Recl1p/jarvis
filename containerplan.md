# Jarvis container consolidation

> **Operational gotcha — read before any `docker exec` into `jarvis-hermes`:** the gateway
> process inside the container runs as user `hermes` (uid 10001), not root. Any `docker exec`
> that writes under `/opt/data` — `hermes auth add`, `hermes gateway restart`, `hermes cron run`,
> and likely any other `hermes` CLI subcommand that writes there — must be run as
> `docker exec --user hermes jarvis-hermes ...` (or `-u 10001`). A plain `docker exec` defaults
> to root and leaves files like `/opt/data/auth.json` owned `root:root`, which the `hermes`-user
> gateway process then can't read — this looks exactly like a real "Provider authentication
> failed / No Anthropic credentials found" error even when the credentials are fine. Hit three
> times in the 2026-09-11 session before the pattern was recognized; see `HANDOFF.md` for the
> incident notes and README.md's Troubleshooting section for the general rule and repair
> commands.

## Goal

Incorporate OpenJarvis capabilities into the existing Hermes Jarvis dashboard at
http://localhost:8642/vesper, with one Jarvis application container. Hermes remains
the main conversation agent. OpenJarvis agent templates, scheduling, lifecycle
controls, results, and model settings appear inside that dashboard. Preserve
existing conversations, credentials, tasks, models, and browser logins.

Ollama remains a supporting model-engine container. OpenJarvis runs as an internal
process in the Hermes application container, with an isolated Python environment;
it does not have a separate application container or exposed port 8000 dashboard.
The browser desktop remains available on localhost:6080.

## Observed starting state

- `jarvis-hermes`: dashboard at localhost:8642; data volume
  `jarvis-hermes-data-final` mounted at `/opt/data`.
- `jarvis-openjarvis`: dashboard/API at localhost:8000; browser desktop at 6080;
  data volume `vesper_openjarvis-data` mounted at `/home/openjarvis/.openjarvis`.
- `vesper-ollama-1`: local model engine with volume `vesper_ollama-data`.
- All three use the existing `vesper_default` network.
- Live Hermes Compose definition: `/var/home/akhdaier/.hermes/jarvis-docker/compose.yaml`.
  Its build context is this source directory. It preserves Documents/Downloads mounts
  and rootless Podman user namespace settings.
- Both application health endpoints returned healthy before changes. Port 8000's
  duplicated dashboard called Hermes's `/vesper/status` endpoint and tried to parse
  the returned HTML as JSON, producing a misleading OFFLINE indicator.

## Implementation decisions

1. Extend the Hermes image with the existing OpenJarvis Python runtime and browser
   dependencies. Keep Python dependencies isolated to avoid breaking either runtime.
2. Supervise OpenJarvis inside the application container and bind its API to loopback.
3. Mount the existing OpenJarvis data volume at `/opt/data/openjarvis`; migrate only
   integration URLs/key paths, preserving user model/auth configuration and task stores.
4. Add an authenticated, allowlisted Hermes HTTP bridge for OpenJarvis controls,
   including streamed model-download progress. Keep the internal credential server-side.
5. Open agent/model dialogs within the Hermes dashboard instead of opening another
   Jarvis dashboard. Keep existing Hermes scheduler controls available.
6. Validate a separate disposable container before touching the live application.
   Back up live configuration/data before switching; retain rollback images/definitions.

## Progress as of 2026-09-10

- [x] Diagnose duplicate dashboard and verify both original services' health.
- [x] Identify live data volumes, network, mounts, and Compose definition.
- [x] Draft combined image, internal service launcher, and integration-settings migration.
- [x] Draft dashboard agent/model dialogs and authenticated API bridge.
- [x] Add HTTP regression tests for authentication, allowed routes, request forwarding,
      streamed downloads, and unavailable-runtime handling.
- [x] First combined image build completed (before final bridge/tests were included).
- [x] Repair distributable Compose definition and validate YAML plus service, port and
      volume relationships locally. Compose-engine validation still needs container access.
- [x] Review runtime packaging and API bridge. Move integration initialization before
      profile-gateway restoration and initialize ownership of the nested OpenJarvis volume.
- [x] Finish UI source review and static JavaScript verification. Keep native controls
      in the same dashboard; prevent repeated submissions and handle request errors.
- [x] Update README and prepare `deployment/compose.live-unified.yaml` without applying it.
- [x] Document consistent backups, cutover checks and rollback in `deployment/MIGRATION.md`.
- [x] Verify first-boot and repeated migration behavior against temporary data directories.
- [x] Build final image and run required gateway tests plus isolated runtime/UI checks.
- [x] Back up live data, stop the old OpenJarvis service, recreate Hermes with both data
      volumes, and verify preserved state and one application dashboard.
- [x] Verify restart behavior and document rollback and final test results.

## Cutover completed 2026-09-11

`jarvis-hermes` now runs `localhost/jarvis-unified:local` against the real
`jarvis-hermes-data-final` and `vesper_openjarvis-data` volumes (the live
`.hermes/jarvis-docker/compose.yaml` already matched this definition; this session
found the cutover already applied and finished verifying it):

- Internal health: `/health` on 8642 and the embedded OpenJarvis `/health` on 8000 both
  return 200 (`{"status":"ok"}`) once `vesper-ollama-1` is running — its 503 during
  validation was traced to the embedded OpenJarvis process failing DNS resolution of
  `ollama` while that container was stopped, not an application bug.
- `localhost:8642/vesper` and `localhost:6080/vnc.html` both return 200 against the real
  container.
- Saved state confirmed present after cutover: `/opt/data/config.yaml`, `/opt/data/auth.json`,
  `/opt/data/openjarvis/dashboard.key`, and the OpenJarvis agent template registry
  (`GET /v1/agents` via the real `dashboard.key`) all resolve correctly.
- `docker restart jarvis-hermes` recovers to `healthy` and both endpoints come back up;
  state files above are unchanged afterward.
- Port 8000 is unpublished (only 6080/8642 are bound on `jarvis-hermes`).
- Old `jarvis-openjarvis` standalone container and the disposable
  `jarvis-unified-validation` container (plus its throwaway
  `jarvis-unified-check-hermes`/`jarvis-unified-check-openjarvis` volumes) have been
  removed at the user's request — the migration is no longer treated as reversible via
  those containers. Rollback beyond this point would rely on the
  `unified-backup-20260910-215740` directory and the `jarvis-hermes:pre-unified` image tag
  (if still present) rather than a live standby container.
- Fixed during this same session: `hermes/gateway/platforms/api_server.py`'s
  `cors_middleware`/`_origin_allowed` was rejecting the dashboard's own same-origin
  `import()` of `cloudflare-speedtest.js` with 403 (browsers send an `Origin` header for
  dynamic module imports even when same-origin); it now also allows a same-origin request
  by comparing `Origin` against the request's `Host`.

Live migration is complete. The stale `jarvis-hermes-data` volume (no `-final` suffix)
was inspected read-only, confirmed byte-identical to `jarvis-hermes-data-final` as of
~19:53 EDT on 2026-09-10 with nothing written since, confirmed unmounted by any
container, and removed.

## Make Ollama optional (done)

Goal: Ollama should not be a hard dependency for the app container to be considered
healthy, and should not auto-start with a plain `docker compose up -d` -- only start it
when the user actually wants a local model.

Root cause found: the container healthcheck (baked into `Dockerfile.hermes` and
duplicated in `compose.yaml`, `deployment/compose.live-unified.yaml`, and the live
`~/.hermes/jarvis-docker/compose.yaml`) required OpenJarvis's own `/health` on port 8000
to return 200. That endpoint is `MultiEngine.health()` = `any(OllamaEngine.health(),
CloudEngine.health())` (`openjarvis/src/openjarvis/engine/multi.py`), and the embedded
OpenJarvis has no cloud credentials of its own configured, so with Ollama stopped both
sub-engines report unhealthy and `/health` 503s -- which then failed the *whole app
container's* Docker health status, even though Hermes itself (port 8642) was fine.

Fix applied to the live container and verified:

- `Dockerfile.hermes` / all three compose files' `healthcheck.test`: still requires
  8642 to return 200, but for 8000 only fails on a genuine connection-level error
  (refused/timeout) -- an HTTP error response (503, "no engine configured") now counts
  as "the process is alive," which is factually what it means.
- Root `compose.yaml` (fresh-install path): removed `depends_on: [ollama]` from
  `hermes`, and gave the `ollama` service `profiles: ["local-model"]` so a bare
  `docker compose up -d` no longer starts it -- `docker compose up -d ollama` (already
  the documented command for local models) remains the way to start it explicitly.
- Explicitly decided against auto-starting the Ollama container from inside the app
  container (would require mounting `/var/run/docker.sock`, giving that container
  control over every container on the host -- rejected as too large a blast-radius
  increase). "Spin up on demand" is deferred to a detect-and-prompt UX in the dashboard
  instead of a literal backend auto-start; that UX has not been built yet.

**Resolved blocker**: the first attempt to apply this (`docker compose up -d --no-deps
hermes`) triggered an image rebuild (Dockerfile changed) that got killed by a command
timeout mid-build, which left rootless Podman's `podman.service` wedged ("Failed to
cleanly shutdown API service: context deadline exceeded" in its journal) -- every
`podman`/`docker` CLI command hung system-wide afterward, even after restarting
`podman.service`. No stale lock was held by any live process and nothing was in
uninterruptible I/O, so it wasn't safe to force-clear without a reboot. `jarvis-hermes`
itself was completely unaffected throughout (dashboard/noVNC kept returning 200); only
the CLI/management plane was wedged. The user rebooted the host (2026-09-10 23:13 EDT),
which cleared it -- `podman ps` responded immediately afterward.

**Verified after reboot**: started `vesper-ollama-1`, ran
`docker compose -f ~/.hermes/jarvis-docker/compose.yaml up -d --no-deps hermes` to
recreate `jarvis-hermes` from the existing image (no rebuild needed -- the live compose
file's `healthcheck:` override applies regardless of the image's baked-in one) --
recreated cleanly, came up `healthy`. Then stopped `vesper-ollama-1` and watched
`jarvis-hermes` across 8 healthcheck cycles (2 minutes): stayed `healthy` throughout,
while `curl 127.0.0.1:8000/health` inside the container correctly returned `503` (no
engine configured) the whole time and `127.0.0.1:8642/health` stayed `200`. Dashboard
(`localhost:8642/vesper`) and noVNC stayed reachable. Ollama is now genuinely optional:
stopped by default, not required for the app to report healthy, and starts only via an
explicit `docker compose up -d ollama` when local models are wanted.

Not yet built: a dashboard-side "Ollama isn't running" detect-and-prompt UX for when a
user selects a local model while Ollama is stopped (deferred, no backend auto-start by
design -- see above).

## Execution and delegation

The user requested continuation with Luna sub-agents and this document. Three
`gpt-5.6-luna` agents were assigned bounded, independent work:

- `integration_review`: authenticated bridge review and HTTP regression tests.
- `dashboard_integration`: same-dashboard agent/model dialogs and request lifecycle.
- `deployment_docs`: unified architecture, build and operation documentation.

The primary agent owns container packaging, initializer migration and tests, live
Compose preparation, backup/rollback documentation, review of results, and this record.
Delegation has not been used to bypass a rejected privileged action.

## Verification completed locally

- `python3 -m unittest discover -s deployment/tests -v`: **2 passed**. These use actual
  temporary data directories to check first boot, repeated initialization, unchanged
  existing credentials/model settings/task-store files, and migrated connection paths.
- Parsed `compose.yaml` and `deployment/compose.live-unified.yaml` using PyYAML and
  checked their application image, command, ports, dependencies and volume mappings.
  No OpenJarvis application service or published port 8000 appears in either definition.
- `sh -n` on initialization and runtime launcher scripts: passed.
- Python syntax compilation/AST checks on the changed initialization and bridge code: passed.
- `node --check` for both new dashboard scripts: passed.
- `node deployment/tests/test_openjarvis_request.mjs`: **5 behavior checks passed** using
  the shipped request helper and a fake HTTP transport. Covers authenticated bridge URL,
  malformed successful responses, API error messages, empty DELETE responses and a
  caller signal that was already aborted. This does not substitute for browser testing.
- Canonical Hermes test runner was attempted for `test_vesper_openjarvis.py`; it stopped
  before running tests because no available virtual environment contains pytest.
  Host Python also lacks aiohttp. This is **not** a passing HTTP test result.

Source backups made before the consolidation edits are in `deployment/legacy/`.
They are source references, not backups of live user data or the live Compose file.
The active Compose file under `.hermes/jarvis-docker/` has not been replaced.

## Live migration progress

Container access was restored after the user confirmed continuation. Both Compose
files passed the Compose engine's configuration validation. Final image:
`404ccec310fc068a264c2603fc2fab58600d9da65e6b7543ff25d1fce9d01ce2`.

Disposable container `jarvis-unified-validation` used separate data volumes and
localhost ports 18642/16080. Results:

- Embedded OpenJarvis/Python/Rust/HTTP/Playwright imports passed.
- Both internal health endpoints returned HTTP 200.
- Canonical Hermes runner: **4 tests passed, 0 failed**, covering the new bridge
  and existing Hermes scheduler controls. Pinned pytest dependencies were installed
  only in the disposable container.
- Real Playwright browser check: OpenJarvis agent and model dialogs opened within
  the Hermes dashboard; local engine available; no JavaScript errors or extra tab.
- Fixed the idle download-cancel button's visibility found during visual validation.
- Native API smoke test: unauthenticated 401, template/model inventories, manual
  agent creation, pause, resume and delete all passed without model inference.
- Graceful restart: both health endpoints recovered and a persistent browser cookie
  survived.

Rollback directory prepared:
`/var/home/akhdaier/.hermes/jarvis-docker/unified-backup-20260910-215740`.
Live cutover is in progress. The old Compose definition and Hermes image tag are
preserved; consistent backups of both live stores are being made after stopping
both application services. No final live success is claimed until checks finish.

## Validation required before cutover

- Parse both Compose definitions and check published ports and persistent volume mapping.
- Build the final image; confirm both native Python imports and service readiness.
- Use `hermes/scripts/run_tests.sh` for the relevant gateway tests in an isolated environment.
- Test dashboard authentication and OpenJarvis templates/list/create/pause/resume/delete
   against disposable state; use no paid inference or user-facing scheduled messages.
- Check model settings and download streaming, browser access, and dashboard interactions.
- Confirm port 8000 is unpublished and the OpenJarvis button stays in the same dashboard.
- Confirm existing Hermes tasks/history and OpenJarvis agents/browser state survive cutover.
- Restart once to check startup ordering, health, and browser profile persistence.

## Post-migration session work (2026-09-11) — see HANDOFF.md and README.md

Separate from the container-consolidation work above, but landed in the same 2026-09-11
session, after "Make Ollama optional" was marked done:

- [x] Custom music-upload feature shipped: new `/vesper/music/tracks` routes in
      `api_server_vesper.py`, a path-scoped exception in `api_server.py`'s
      `body_limit_middleware` for the larger upload size, and a MY MUSIC / LIVE RADIO toggle
      plus upload/rename/delete UI in `dashboard.dc.html`. Full description in README.md's
      "Custom music library" section; original design intent in
      `~/.claude/plans/pure-popping-garden.md`.
- [x] Several AUDIO/chat panel UI fixes (message-wrap width, speaker-label column width, core
      reposition, a `componentDidUpdate` console-spam bug in the custom "dc" component
      framework). Details in `HANDOFF.md`.
- [x] Diagnosed and fixed a recurring `/opt/data/auth.json` ownership-corruption issue caused by
      running `docker exec` as root instead of `--user hermes` — see the callout at the top of
      this file, README.md's Troubleshooting section, and `HANDOFF.md` for the full incident
      history.
- [x] Clarified cron job semantics: `enabled: false` + `state: "completed"` with
      `repeat: {times: N, completed: N}` is normal completion, not a stuck job.

**Open loop**: the auth-permission fix is verified from the backend (stable through a real chat
exchange and a real cron run), but the user has not yet personally confirmed in their own browser
that chat works. Don't treat that as closed until they confirm. See `HANDOFF.md` for full status,
including two unrelated low-priority items (OpenRouter/Nous provider billing, an unconfirmed
`BRAVE_SEARCH_API_KEY` offer).
