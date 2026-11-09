# JARVIS stack (TrueNAS)

JARVIS 2: the dashboard, voice and browser desktop for Hermes Agent. Since 2026-11-09 Hermes is the TrueNAS app
`hermes` (catalog app hermes-agent) on this stack's data folder, so JARVIS and Hermes share one memory. `compose.yaml`
explains the layout (image, Hermes app, data, files, access, CLI). The project is github.com/Recl1p/jarvis: its
`README.md` says how it works, `INSTALL.md` how friends install it, and `HANDOFF.md` has the history and gotchas
(entry 38: the move to the TrueNAS app).

- Setup screen, connection test, Hermes updates, password: http://truenas.local:8642/jarvis/
- Dashboard: http://truenas.local:8642/vesper, or https://truenas-scale.anchovy-chameleon.ts.net/vesper (Tailscale)
- Hermes's own dashboard: http://truenas.local:30433 (its login is in the app's settings in TrueNAS)
- **Update JARVIS:** press **Update** on the stack in Dockge (pulls the newest `ghcr.io/recl1p/jarvis:2` and restarts).
  Or `sudo sh update.sh`: the same, plus a health check and an automatic rollback to the image that ran before.
- **Update Hermes:** the Updates card on JARVIS's setup screen (also daily at the hour set there), or Apps > hermes >
  Update in TrueNAS. JARVIS's way snapshots the data first, checks the dashboard's connector answers, and rolls the app
  back if it doesn't.
- `.env` (private, 0600): `JARVIS_TRUENAS_API_KEY`, the key of TrueNAS user `jarvis-updater` (APPS_WRITE only).
- Backups: snapshots of the `SSDCACHE256/jarvis` dataset.
- `cutover.sh`, `rehearse.sh` and `rollback.sh` are the JARVIS 1 -> 2 move of 2026-10-23, kept for the record.
