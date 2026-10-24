# JARVIS stack (TrueNAS)

JARVIS 2: the dashboard, voice and browser desktop for Hermes Agent, with Hermes installed by JARVIS (official
installer) into the data volume. `compose.yaml` explains the layout (data, files, access, updates, CLI). The project
is in `src/`: `src/README.md` (how it works), `src/SHARING.md` (running it elsewhere), `src/HANDOFF.md` (history and
gotchas; entry 34 covers JARVIS 2).

- Setup screen, connection test, Hermes updates: http://truenas.local:8642/jarvis/
- Dashboard: http://truenas.local:8642/vesper, or https://truenas-scale.anchovy-chameleon.ts.net/vesper (Tailscale)
- Update JARVIS after code changes in `src/`: `sudo sh update.sh` (smoke-tests the new image, rolls back by itself).
  Hermes updates itself daily (setup screen, Updates).
- Backups: snapshots of the `SSDCACHE256/jarvis` dataset.
- Back to JARVIS 1: `sudo sh rollback.sh /mnt/hdd_2tb/configs/jarvis1-backup-<stamp>` (made by the cutover).
