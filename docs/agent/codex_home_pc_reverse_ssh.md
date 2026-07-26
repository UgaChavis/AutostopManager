# Codex Home PC Reverse SSH Access

Server-side Codex has working command and filesystem access to the owner's home
Windows PC.

This file also routes the separate multi-device `managed-pc` fleet. Keep the
legacy `home-pc` route and managed fleet independent: do not reuse or rotate one
route's keys while operating on the other.

## Managed Windows PC Fleet

The server-side control plane is deployed from the separate version-controlled
project `/opt/autostop-managed-pc`.

- Primary transport: outbound reverse SSH; Windows opens no public or LAN SSH.
- Enrollment: owner-controlled `SECRET_KEY\AUTOSTOP_REMOTE` USB bundle.
- Server CLI: `managed-pc`.
- Per device: unique tunnel key, loopback listener, server-to-device key and
  pinned Windows host key.
- Runtime state: `/var/lib/autostop-managed-pc`; never print or copy its private
  keys or the USB enrollment credential.
- Server health: `managed-pc doctor` and
  `autostop-managed-pc-health.timer`.

### Named AutoStop service workstations

Both entries below are owner-authorized managed Windows PCs for exact
administration and operational support. They are separate from the legacy
`home-pc` route and from each other: never reuse credentials, listener ports,
or a prior status result between them.

| Managed-PC alias | Hostname | Physical role |
| --- | --- | --- |
| `desktop-e0e84lt` | `DESKTOP-E0E84LT` | AutoStop service reception (ресепшен) |
| `Компьютер механиков` | `WIN-CRINTQ55M38` | AutoStop mechanics' workstation |

- Resolve the requested role to the exact alias, run `managed-pc doctor`, then
  `managed-pc status <alias>`. Only after a successful status check may
  `shell`, `run`, `powershell`, Codex, browser, or file-copy commands be used
  for the owner-requested task.
- Do not rely on a remembered listener port, hostname, or health result:
  reread current status every time. Keep all managed-PC safety limits in force,
  including no reboot, shutdown, destructive change, or service stoppage
  without the owner's separate instruction.
- A sleeping, powered-off, or disconnected PC cannot be repaired remotely. Do
  not re-enroll it or rotate keys for that condition. An on-site administrator
  wakes the PC and starts `\Autostop\AutostopCodexRemoteTunnel`; only resume
  remote work after `managed-pc status <alias>` confirms `ssh_ok=true`.
- A no-sleep power-policy change is an owner-requested machine setting, not an
  assumption. It must be applied and reread only when the owner asks for that
  exact device.

Normal workflow:

```bash
managed-pc list
managed-pc newest
managed-pc status <alias>
managed-pc inspect <alias>
managed-pc codex-status <alias>
managed-pc codex-login <alias>
managed-pc codex-run <alias> --prompt-file /path/task.md --mode read-only
managed-pc codex-result <alias> <job-id>
managed-pc shell <alias>
managed-pc run <alias> -- <program> <args...>
managed-pc powershell <alias> <local-script.ps1>
managed-pc copy-to <alias> <source> <destination>
managed-pc copy-from <alias> <source> <destination>
managed-pc repair <alias>
managed-pc rename <alias> <new-alias>
managed-pc revoke <alias>
managed-pc audit <alias>
```

Run `managed-pc status <alias>` before a normal operation. Resolve the alias to
the exact device and report which machine was affected. The owner has authorized
full administrative work on enrolled machines, but formatting, bootloader
changes, mass deletion, disabling protection, reboot/shutdown, and stopping
critical business services still require a separate exact instruction.

USB preparation and rotation:

```bash
managed-pc prepare-usb --output /safe/staging/path
managed-pc verify-usb /safe/staging/path/AUTOSTOP_REMOTE
managed-pc rotate-usb-credential
```

The USB credential can enroll a new machine but cannot access existing machines
or obtain a server shell. If the USB is lost, rotate it; existing devices remain
connected. The complete implementation, rollback commands and Windows file list
are documented in `/opt/autostop-managed-pc/README.md`.

Server-side SSH and SCP calls reuse a root-only control socket for up to ten
minutes. This avoids a new SSH handshake for each command or transfer. After a
control-plane upgrade, regenerate active device SSH configs with:

```bash
managed-pc refresh-device-files
```

Refreshing, revoking, or re-enrolling closes any existing control session before
keys or authorization change. The Windows maintenance account may create only a
local forward to `127.0.0.1:9223`, reserved for the dedicated AutoStop CRM Chrome
diagnostics profile; it cannot forward to arbitrary LAN or internet targets.

The optional Windows Codex CLI worker is on-demand only. Run `codex-status`
first, send each prompt as a file with `codex-run`, keep `read-only` as the
default, and select `workspace-write` or `full` explicitly per job. Full setup,
log, authentication and rollback details live in
`/opt/autostop-managed-pc/README.md`.

The fleet control-plane source is the private repository
`https://github.com/UgaChavis/autostop-managed-pc`. Before deploying a change,
commit and push the verified working tree; then run
`/opt/autostop-managed-pc/deploy/install.sh`, `managed-pc doctor`, and an
exact-device `managed-pc status`/`managed-pc codex-status` check. The health
timer `autostop-managed-pc-health.timer` is the server-side daily readiness
gate; it must stay enabled and active. Do not put enrollment credentials,
private keys, prompt files, job results, or Windows logs into this repository.

## Current Route

- Connect from this server with `ssh home-pc`.
- Home PC: `DESKTOP-BUSO4I8`, Windows OpenSSH, user `codexadmin`.
- Reverse listener on server: `127.0.0.1:22220`.
- Home SSH listens only on `127.0.0.1:22`; no router port-forward or public
  home SSH (`no public home SSH`).
- Home PC keeps an outbound SSH tunnel as server user `codex-home-tunnel`.
- `ssh`, `sftp`, and `scp` work from this server through alias `home-pc`.
- Verified tools available to `codexadmin`: PowerShell 7.6.3 (`pwsh`),
  Python 3.14.6 (`python`, `pip`), Git, GitHub CLI, Node.js/npm, curl, and tar.
- Helper scripts live in `C:\ProgramData\CodexRemote\bin`.
- Private bootstrap/key material is outside git:
  `/root/codex-home-remote/bootstrap/current`.

Do not print, commit, rotate, or overwrite private keys or
`codexadmin-password.txt`. Do not rotate `home-pc` keys unless Windows and
server sides are updated together.

## Server Files

- `/root/.ssh/config`: alias `home-pc`.
- `/root/.ssh/codex_home_ed25519`: server-to-home key.
- `/etc/ssh/sshd_config.d/90-codex-home-tunnel.conf`: restricts
  `codex-home-tunnel` to remote forwarding on `127.0.0.1:22220`.
- `scripts/codex_home_pc_bootstrap.ps1`: canonical Windows bootstrap script.

## Interactive Route Performance

The legacy `home-pc` alias uses a root-only OpenSSH control socket:

```sshconfig
ControlMaster auto
ControlPath /root/.ssh/controlmasters/%C
ControlPersist 10m
```

Keep `/root/.ssh/controlmasters` at mode `0700` and `/root/.ssh/config` at
`0600`. Check or close the shared connection with:

```bash
ssh -O check home-pc
ssh -O exit home-pc
```

Do not add `ControlMaster` to the Windows OpenSSH client configuration. The
Win32 client is not the supported side of this optimization; multiplexing is
done by the Linux server when it connects to `home-pc`.

For interactive Codex work on the VPS, use:

```bash
codex-session
codex-session /path/to/workspace
```

`/usr/local/bin/codex-session` points to `scripts/codex-session`. It creates or
reattaches the tmux session `codex-main`, so a home ISP or VPN interruption does
not terminate the running Codex CLI. Override the name only when necessary with
`CODEX_TMUX_SESSION`.

The VPS uses the static Cloudflare resolvers `1.1.1.1` and `1.0.0.1` in
`/etc/resolv.conf`. Before changing resolvers again, save the current file under
`/root/autostop-route-backups/<UTC timestamp>/` and compare repeated DNS plus
OpenAI HTTPS timings. Existing containers keep their embedded Docker resolver
state until they are recreated; do not restart production containers solely to
refresh this route.

The home AmneziaVPN route keeps MTU `1280`. These canonical tasks must remain
enabled and point to the stable PowerShell executable
`C:\Program Files\PowerShell\7\pwsh.exe` and scripts under
`%LOCALAPPDATA%\AutostopVPN` for the active desktop user:

- `AutostopVPN Codex client optimization`
- `AutostopVPN Recovery Checks`
- `AutostopVPN Daily Deep Check`

The obsolete `AutostopVPN-RecoveryChecks` task stays disabled. Do not weaken
packet-loss or OpenAI reachability checks to obtain a green result; repeat the
short check to distinguish a transient provider event from a persistent fault.

## Quick Check

```bash
ss -ltnp | rg '127\.0\.0\.1:22220'
ssh -o BatchMode=yes home-pc 'cmd /c echo HOME_PC_OK && hostname && whoami'
printf 'pwd\nquit\n' | sftp -o BatchMode=yes -b - home-pc
ssh -o BatchMode=yes home-pc 'pwsh -NoLogo -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
ssh -o BatchMode=yes home-pc 'python --version && python -m pip --version'
```

Expected:

```text
HOME_PC_OK
DESKTOP-BUSO4I8
desktop-buso4i8\codexadmin
7.6.3
Python 3.14.6
```

If the tunnel is down, check Windows task `\Autostop\CodexRemoteReverseTunnel`
and `C:\ProgramData\CodexRemote\logs\reverse-tunnel.log`. Old `code=255` lines
are not current failures if a later `starting reverse tunnel` has no later
exit.

## Operating Workflow

When the owner asks to interact with the home PC, open this file first. Then:

1. Run the quick check above.
2. Use `ssh home-pc` or `ssh home-pc 'pwsh ...'` for commands.
3. Use `scp`/`sftp` for file transfer.
4. Use `C:\ProgramData\CodexRemote\bin\write-public-desktop-note.ps1` for a
   visible text note on the shared desktop.
5. Use `C:\ProgramData\CodexRemote\bin\open-in-user-session.ps1 -FilePath ...`
   to open a file in the active Windows console session through a one-time
   Scheduled Task.

Run `C:\ProgramData\CodexRemote\bin\health-check.ps1` for the compact current
state. Optional tools not guaranteed for `codexadmin`: VS Code CLI, Chocolatey,
7-Zip CLI, `rg`, and `jq`.

## Windows Bootstrap

Use `scripts/codex_home_pc_bootstrap.ps1` only for setup/repair. It installs
OpenSSH Server, sets loopback-only SSH, creates/reuses `codexadmin`, installs
the server public key, stores the tunnel key under ProgramData, and registers
the SYSTEM scheduled task.

The home-side Codex/PowerShell session must run elevated. Report only compact
status; never paste private key, password, or token contents.

## Rollback

Server side:

```bash
rm -f /etc/ssh/sshd_config.d/90-codex-home-tunnel.conf
systemctl reload ssh
userdel -r codex-home-tunnel
sed -i '/^Host home-pc$/,/^$/d' /root/.ssh/config
```

Windows side:

```powershell
Unregister-ScheduledTask -TaskName CodexRemoteReverseTunnel -TaskPath \Autostop\ -Confirm:$false
Stop-Service sshd
```
