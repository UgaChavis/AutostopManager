# Server and Remote Windows Access

Canonical compact route for AutoStop servers and owner-authorized Windows PCs.
Runtime reachability is never durable documentation: resolve the live target and
verify its identity before every operation.

## Sources Of Truth

- Live SSH aliases and host-key policy: `/root/.ssh/config` and its configured
  `UserKnownHostsFile`.
- FST.KZ VPN access: `/root/.codex/CODEX_VPN_FST_ACCESS.md`; read it first for
  every FST.KZ task and use `autostop-vpn-fst`.
- Managed Windows fleet: `/opt/autostop-managed-pc/README.md`, runtime CLI
  `managed-pc`, and `/var/lib/autostop-managed-pc`.
- Legacy home-PC setup/repair: `scripts/codex_home_pc_bootstrap.ps1`.

Never copy private keys, passwords, tokens, VPN URLs, client profiles, USB
credentials, or protected runtime state into docs, Git, commands, or reports.

## Known Routes

| Target | Route | Live identity check |
| --- | --- | --- |
| Manager/CRM VPS | local shell | `hostname; id -un` |
| FST.KZ VPN server | `autostop-vpn-fst` | documented BatchMode check below |
| Legacy VPS endpoints | `autostop-vps27560`, `autostop-vps27560-alt` | BatchMode only; strict host-key verification |
| AutoStop reception PC | `managed-pc` alias `desktop-e0e84lt` | `DESKTOP-E0E84LT` |
| AutoStop mechanics PC | `managed-pc` alias `Компьютер механиков` | `WIN-CRINTQ55M38` |
| Owner home PC | `home-pc` | `DESKTOP-BUSO4I8`, user `codexadmin` |

`github.com-autostopcrm` is a Git transport alias, not an administrative server
shell. VPN peers are clients, not remote-administration targets.

## Universal Workflow

1. Read the target-specific source above.
2. Start with a bounded read-only identity/status check using `BatchMode=yes`.
3. Stop on an unexpected hostname, user, target id, or SSH host key. Never use
   `StrictHostKeyChecking=no`, accept a new key, or edit `known_hosts` without
   independent owner-approved verification.
4. Perform only the exact owner-authorized operation. Reboot, shutdown,
   destructive changes, key rotation, network changes, and critical-service
   stops need a separate exact instruction.
5. Reread the exact target and report the affected machine and result.

Do not route CRM traffic through a VPN or change CRM networking during VPN
work. Keep all server, managed-PC, and legacy home-PC credentials independent.

## FST.KZ VPN Server

Read `/root/.codex/CODEX_VPN_FST_ACCESS.md` first; it owns the current server
identity, restrictions, and revocation procedure. The normal read-only check is:

```bash
ssh -o BatchMode=yes autostop-vpn-fst \
  'hostname; id -un; docker inspect -f "{{.State.Status}}" amnezia-awg2'
```

Do not duplicate its IP, port, VPN parameters, backup paths, or secret-material
locations here. Do not change the FST server or VPN container unless the owner
asks for that exact change and the external instruction's safeguards pass.

## Legacy VPS Aliases

Alias presence is not proof of access. Treat both legacy aliases as unverified
until the pinned host key and remote identity pass:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 autostop-vps27560 \
  'hostname; id -un'
ssh -o BatchMode=yes -o ConnectTimeout=8 autostop-vps27560-alt \
  'hostname; id -un'
```

On host-key mismatch, stop and report it. Do not bypass the check or infer that
the two addresses still belong to the historical server.

## Managed Windows Fleet

The fleet uses outbound reverse SSH; Windows exposes no public or LAN SSH.
Each device has independent keys, a loopback listener, and a pinned host key.

```bash
managed-pc doctor
managed-pc list
managed-pc status <exact-alias>
```

Require `tunnel_up=true`, `ssh_ok=true`, and the expected hostname before using
`shell`, `run`, `powershell`, `copy-to`, `copy-from`, `codex-status`, or another
exact-device command. Use `/opt/autostop-managed-pc/README.md` for enrollment,
repair, revoke, credential rotation, deployment, and rollback details.

If a PC is asleep, powered off, or disconnected, do not re-enroll it or rotate
keys. Wake it on site and start `\Autostop\AutostopCodexRemoteTunnel`, then
repeat `managed-pc status`. After a control-plane upgrade run
`managed-pc refresh-device-files` and recheck each affected device.

Root-only SSH control sessions use `ControlPersist 600`. The Windows
maintenance account may forward only to `127.0.0.1:9223` for the dedicated CRM
Chrome diagnostics profile.

## Legacy Home PC

`home-pc` is independent of the managed fleet. It reaches Windows OpenSSH
through the loopback reverse listener `127.0.0.1:22220`; there is no public
route (`no public home SSH`). The remote user is `codexadmin`, and the tunnel account is
`codex-home-tunnel`.

```bash
ss -ltnp | rg '127\.0\.0\.1:22220'
ssh -o BatchMode=yes -o ConnectTimeout=8 home-pc \
  'cmd /c echo HOME_PC_OK && hostname && whoami'
printf 'pwd\nquit\n' | sftp -o BatchMode=yes -b - home-pc
ssh -o BatchMode=yes home-pc \
  'pwsh -NoLogo -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
```

`ssh`, `scp`, `sftp`, and `pwsh` are supported. The last verified toolset also
included PowerShell 7.6.3 and Python 3.14.6, but versions must be reread before
relying on them. Visible-desktop helpers are
`write-public-desktop-note.ps1` and `open-in-user-session.ps1`; run
`health-check.ps1` for compact state.

If the listener is absent, the PC is not currently reachable. Wake it on site
and start `\Autostop\CodexRemoteReverseTunnel`; do not rotate keys as an outage
repair. Setup/repair uses the canonical bootstrap script in an elevated Windows
session. Do not rotate or overwrite home-PC key material unless both ends are
updated together.

## Reporting

Report configured access separately from currently verified access. Do not
persist transient host state, peer lists, profiles, credentials, or full remote
outputs in Manager memory or documentation.
