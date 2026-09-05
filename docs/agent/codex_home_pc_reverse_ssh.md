# Server and Remote Windows Access

Resolve one owner-authorized target, inspect `ssh -G <alias>`, then use a bounded
`BatchMode=yes` identity/status probe. Stop on an unexpected host, user or host
key; report configured access separately from reachability observed now.

- Main VPS: `autostop-vps27560`; do not silently switch to its alternate alias.
- FST.KZ: read `/root/.codex/CODEX_VPN_FST_ACCESS.md` and use
  `autostop-vpn-fst` without routing CRM through it.
- Managed fleet: follow `/opt/autostop-managed-pc/README.md`, then `managed-pc
  doctor`, `list` and `status <exact-alias>` as useful.
- Legacy `home-pc`: `DESKTOP-BUSO4I8` as `codexadmin` through
  `127.0.0.1:22220`; its tunnel account is `codex-home-tunnel`.

For the legacy PC, a normal probe is:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 home-pc 'cmd /c echo HOME_PC_OK && hostname && whoami'
```

If absent, the on-site recovery is to wake it and start
`\Autostop\CodexRemoteReverseTunnel`. Verify the live VPS address before using
`scripts/codex_home_pc_bootstrap.ps1`. Reboot, shutdown, key/network changes and
critical-service stops need a separate exact instruction; do not rotate or
accept keys merely to clear an outage. Keep credentials, profiles and remote
output out of Git, documentation and Manager memory.
