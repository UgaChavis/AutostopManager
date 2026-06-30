# Codex Home PC Reverse SSH Access

Server-side Codex has working command and filesystem access to the owner's home
Windows PC.

## Current Route

- Connect from this server with `ssh home-pc`.
- Home PC: `DESKTOP-BUSO4I8`, Windows OpenSSH, user `codexadmin`.
- Reverse listener on server: `127.0.0.1:22220`.
- Home SSH listens only on `127.0.0.1:22`; no router port-forward or public
  home SSH.
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
