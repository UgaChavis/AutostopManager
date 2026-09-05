# Server and Remote Windows Access

Start from one owner-authorized target. Use `ssh -G <alias>` to explain
configuration and a noninteractive probe only when current reachability matters.
Keep configured access separate from observed availability; stop on an unexpected host, user or key.

- Main VPS: use `autostop-vps27560`; do not silently switch to its alternate alias.
- Managed fleet: follow `/opt/autostop-managed-pc/README.md`.
- After connection, legacy `home-pc` should report `DESKTOP-BUSO4I8`; access details live in SSH configuration.

If `home-pc` is unavailable, report it. Bootstrap or recovery via `scripts/codex_home_pc_bootstrap.ps1` is a separate exact change; verify its `ServerHost` then. Do not accept or rotate keys, reboot, alter networking or stop critical services merely to clear an outage. Keep credentials, profiles and remote output out of Git, docs and Manager memory.
