# Codex Home PC Reverse SSH Access

Use this route when the owner wants this server-side Codex agent to operate a
Windows 10/11 home PC with filesystem and process access, without exposing SSH
on the home router or public internet.

## Architecture

- Home PC runs Windows OpenSSH Server on `127.0.0.1:22` only.
- Home PC holds a persistent outbound SSH session to this server as
  `codex-home-tunnel`.
- The outbound session creates a server-local reverse listener:
  `127.0.0.1:22220 -> home-pc 127.0.0.1:22`.
- Server-side Codex connects with `ssh home-pc`.
- The Windows user is `codexadmin` and belongs to local Administrators.
- Private bootstrap material lives outside git:
  `/root/codex-home-remote/bootstrap/current`.

Do not paste private key contents into chat or commit them to git.

## Server State

Expected server-side pieces:

- SSH alias: `/root/.ssh/config`, host `home-pc`.
- Server-to-home key: `/root/.ssh/codex_home_ed25519`.
- Reverse tunnel user: `codex-home-tunnel`.
- Tunnel policy: `/etc/ssh/sshd_config.d/90-codex-home-tunnel.conf`.
- Windows bootstrap bundle:
  `/root/codex-home-remote/bootstrap/current`.

The tunnel user is restricted to public-key auth and remote port forwarding for
`127.0.0.1:22220`. It must not receive shell, TTY, agent forwarding, public
listen addresses, or password auth.

## Prompt For The Home Windows Codex Agent

Paste this prompt into Codex running on the home Windows PC from an elevated
administrator session:

```text
You are Codex running on this Windows 10/11 home PC with administrator rights.
Set up the prepared Autostop Codex reverse SSH access. Work autonomously, but do
not print private keys, passwords, or full secret file contents.

Goal:
- Download the prepared bootstrap bundle from the Autostop server.
- Run the Windows bootstrap script from the bundle.
- Configure OpenSSH Server to listen only on 127.0.0.1.
- Create or reuse local admin user codexadmin.
- Install the server public key for codexadmin.
- Install the reverse tunnel private key locally under ProgramData.
- Register and start a SYSTEM scheduled task named
  \Autostop\CodexRemoteReverseTunnel.
- Report only status lines and any non-secret errors.

Use this default server route:
- host: 46.8.254.243
- user: root
- bundle: /root/codex-home-remote/bootstrap/current

Implementation:
1. Confirm the session is elevated. If not elevated, stop and tell the owner to
   restart Codex/PowerShell as Administrator.
2. Create C:\ProgramData\CodexRemote\bootstrap.
3. Use the existing SSH access from this home PC to the server to download the
   bundle. Start with:
   scp -r root@46.8.254.243:/root/codex-home-remote/bootstrap/current/* C:\ProgramData\CodexRemote\bootstrap\
   If that host/user is not the configured SSH route on this PC, inspect the
   local SSH config and use the existing server alias. Do not print key content.
4. Run:
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\CodexRemote\bootstrap\codex_home_pc_bootstrap.ps1
5. After it finishes, verify:
   - Get-Service sshd
   - Get-NetTCPConnection -LocalPort 22 -State Listen
   - Get-ScheduledTask -TaskName CodexRemoteReverseTunnel -TaskPath \Autostop\
   - Get-Content C:\ProgramData\CodexRemote\logs\reverse-tunnel.log -Tail 20
6. Send back only compact status and errors. Do not paste:
   - C:\ProgramData\CodexRemote\ssh\home_reverse_to_server_ed25519
   - C:\ProgramData\CodexRemote\secrets\codexadmin-password.txt
   - any private SSH key or password.
```

## Server Verification After Home Bootstrap

Run from `/opt/AutostopManager` on this server:

```bash
ss -ltnp | rg '127\.0\.0\.1:22220'
ssh -o BatchMode=yes home-pc 'hostname && whoami'
ssh home-pc 'powershell -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
ssh home-pc 'powershell -NoProfile -Command "New-Item -ItemType Directory -Force C:\ProgramData\CodexRemote\probe | Out-Null; Set-Content C:\ProgramData\CodexRemote\probe\server_probe.txt ok; Get-Content C:\ProgramData\CodexRemote\probe\server_probe.txt"'
```

Expected results:

- `127.0.0.1:22220` listens only on loopback.
- `ssh home-pc` authenticates as `codexadmin`.
- PowerShell commands run on the home PC.
- Test file write/read under `C:\ProgramData\CodexRemote\probe` returns `ok`.

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
# Restore C:\ProgramData\ssh\sshd_config from its .codex-remote.bak timestamp if needed.
```
