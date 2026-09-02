# Server and Remote Windows Access

Compact recovery route for owner-authorized servers and Windows PCs. Resolve
live reachability and verify the exact target before every operation.

## Canonical Owners

- SSH aliases and host keys: `/root/.ssh/config` and its `UserKnownHostsFile`.
- General server endpoints: `autostop-vps27560` and
  `autostop-vps27560-alt`; never switch to the alternate alias automatically.
- FST.KZ: `/root/.codex/CODEX_VPN_FST_ACCESS.md` and `AGENTS.md`; use
  `autostop-vpn-fst` only after reading them.
- Managed fleet and reception printing: `/opt/autostop-managed-pc/README.md`
  and the `managed-pc` CLI.
- Legacy home-PC setup/repair: `scripts/codex_home_pc_bootstrap.ps1`.

Never copy credentials, keys, VPN profiles, protected runtime state, or full
remote output into Git, docs, reports, or Manager memory.

## Universal Workflow

1. Read the target-specific owner above.
2. Resolve the live endpoint with `ssh -G <alias>`, then start with a bounded
   identity/status check using `BatchMode=yes`.
3. Stop on an unexpected hostname, user, target ID, or host-key mismatch. Never
   bypass verification, accept an unverified key, or edit `known_hosts` blindly.
4. Perform only the exact authorized operation. Reboot, shutdown, destructive
   changes, key rotation, network changes, and critical-service stops require a
   separate exact instruction.
5. Reread the exact target and report the affected machine and result.

Do not route CRM traffic through a VPN or mix credentials between servers,
managed PCs, and the legacy home PC.

## Managed And VPN Routes

For FST.KZ, the external access document owns identity, commands, restrictions,
and recovery. For managed PCs, use `managed-pc doctor`, `list`, then `status` on
the exact alias; the managed-PC README owns enrollment, recovery, file transfer,
printing, deployment, and rollback. Do not copy those procedures here.
A main-VPS IP change separately requires a managed-PC endpoint refresh and new
client files; editing this document alone cannot restore their tunnels.

## Legacy Home PC

`home-pc` is independent of the managed fleet. It reaches `DESKTOP-BUSO4I8` as
`codexadmin` through the loopback reverse listener `127.0.0.1:22220`; the tunnel
account is `codex-home-tunnel`. There is no public home SSH route.
Before rerunning the bootstrap, verify its `ServerHost` against the live public
VPS address and pass the parameter explicitly if they differ.

```bash
ss -ltnp | rg '127\.0\.0\.1:22220'
ssh -o BatchMode=yes -o ConnectTimeout=8 home-pc \
  'cmd /c echo HOME_PC_OK && hostname && whoami'
printf 'pwd\nquit\n' | sftp -o BatchMode=yes -b - home-pc
ssh -o BatchMode=yes home-pc \
  'pwsh -NoLogo -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
```

If the listener is absent, wake the PC on site and start the scheduled task
`\Autostop\CodexRemoteReverseTunnel`, then repeat the probes. Do not rotate keys
as an outage repair. Run the bootstrap only in an elevated Windows session, and
do not rotate or overwrite home-PC key material unless both ends are updated
together.

## Reporting

Report configured access separately from currently verified access. Keep
transient host state and private remote output out of durable documentation.
