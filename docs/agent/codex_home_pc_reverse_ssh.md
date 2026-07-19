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
