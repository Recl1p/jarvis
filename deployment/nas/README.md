# JARVIS stack (TrueNAS)

JARVIS 2: the dashboard, voice and browser desktop for Hermes Agent, with Hermes installed by JARVIS (official
installer) into the data volume. `compose.yaml` explains the layout (image, data, files, access, CLI). The project is
github.com/Recl1p/jarvis (private): its `README.md` says how it works, `INSTALL.md` how friends install it, and
`HANDOFF.md` has the history and gotchas.

- Setup screen, connection test, Hermes updates, OmniVoice, password: http://truenas.local:8642/jarvis/
- Dashboard: http://truenas.local:8642/vesper, or https://truenas-scale.anchovy-chameleon.ts.net/vesper (Tailscale)
- **Update JARVIS:** press **Update** on the stack in Dockge (pulls the newest `ghcr.io/recl1p/jarvis:2` and restarts).
  Or `sudo sh update.sh`: the same, plus a health check and an automatic rollback to the image that ran before.
  Hermes updates itself daily (setup screen, Updates).
- Backups: snapshots of the `SSDCACHE256/jarvis` dataset.
- Back to JARVIS 1: `sudo sh rollback.sh /mnt/hdd_2tb/configs/jarvis1-backup-<stamp>` (made by the cutover;
  `cutover.sh` and `rehearse.sh` are the JARVIS 1 -> 2 move of 2026-10-23, kept for the record).
