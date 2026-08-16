# Telegram Workflow Playbook

Purpose: operate the owner's personal Telegram account through the private
AutoStopManager Telethon bridge with exact consent, bounded reads, verified
writes, and no durable copy of private content or credentials.

Telegram is the live source of truth for its dialogs, messages, groups,
channels, contacts, media metadata, and authorization state. Manager stores
only this route and de-identified technical verification.

## Runtime

- Service: `autostop-telegram.service`.
- CLI module: `autostop_manager.telegram_bridge` in
  `/opt/autostop-telegram-venv` with the active immutable Manager release as
  `PYTHONPATH` and working directory.
- Credentials: root/service-controlled file under `/etc/autostop-telegram`.
- Session: root/service-controlled SQLite file under
  `/var/lib/autostop-telegram`.
- Local RPC: mode-0600 Unix socket under `/run/autostop-telegram`.

Never print, copy, attach, back up to Git, or persist credential values, QR
tokens, 2FA, login codes, contract tokens, or the session file. A Telethon
session contains the authorization key and can grant account access.

## Fast Start

Run commands as `autostop-telegram`; return only bounded, task-relevant fields.

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-manager-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge status
```

Current read surface:

- `status`
- `dialogs --limit N`
- `search --query TEXT --limit N`
- `read --peer ID --limit N`

Current write surface:

- `send --peer ID --text TEXT --mode dry_run`
- `send --peer ID --text TEXT --mode apply --contract-token TOKEN --idempotency-key KEY`
- `send-photo --peer ID --file /run/autostop-telegram/outbox/NAME.jpg --caption TEXT --mode dry_run`
- `send-photo --peer ID --file ... --caption TEXT --mode apply --contract-token TOKEN --idempotency-key KEY`

Use the local daemon for normal work. Do not open the same SQLite session with
a second Telethon client while the daemon is active. Stop the daemon only for a
bounded authorization or direct diagnostic that cannot use the local RPC, and
restore it immediately.

## Read Workflow

1. Check service active and `authorized=true`.
2. Use `dialogs` for a recent overview or `search` for a name/username.
3. Classify candidates by `kind`, exact title, contact status, and numeric ID.
4. If one exact person/group is identified, use its numeric ID for all later
   calls. Never keep resolving a known target by display name.
5. Read the smallest useful window, normally 3-20 messages.
6. Summarize private content. Do not paste full threads, large channel feeds,
   contact tables, or message exports into chat or project files.

`read` redacts `vpn://` and `tg://` credential-bearing URIs by default. Keep
that redaction in any derived output. Do not treat reading as permission to
send, forward, delete, edit, join, leave, block, download media, or change read
state.

## Send Workflow

A send is authorized only when the owner names the intended recipient and
message text or clearly delegates wording in the active request.

1. Search the target and resolve exactly one numeric peer ID. A positive ID is
   normally a private user; negative IDs are groups/channels. `kind` is the
   authoritative bridge hint.
2. Focused-read the target by ID. If candidates remain ambiguous, stop and ask
   the owner; do not infer identity from a similar username.
3. Freeze the exact message text. Make agent authorship clear when relevant.
4. Call `send` in `dry_run` mode. Check target ID/title, message length,
   conversation tail, and contract creation.
5. Call `apply` once with unchanged target/text, the returned contract token,
   and a fresh idempotency key.
6. Require bridge readback, then independently read the exact dialog and match
   the outgoing message ID/text.

If the apply response times out or is lost, outcome is unknown. Do not issue a
new key or resend. First read the exact target and reconcile the full outgoing
text; resend only after confirmed absence. This rule prevents duplicates.

Photo sends follow the same exact-target, dry-run/apply, unchanged contract,
fresh idempotency, and independent readback sequence. Stage only one JPEG in
the service-owned mode-0700 `/run/autostop-telegram/outbox` directory; the file
must be service-owned mode 0600, at most 10 MiB, and have an explicit caption.
Do not accept a symlink, relative path, another directory, video, document, or
arbitrary media type. After verified delivery, unlink that exact staged copy;
on an unknown outcome, retain it only until the dialog is reconciled. Never
use a real photo send as a health check.

## QR And 2FA Recovery

Telegram QR login tokens usually expire in about 30 seconds. Generate a new
token only after its server-provided `expires` time. Write the PNG atomically,
show a unique snapshot path to avoid UI caching, and keep the waiting process
alive before the phone scans it.

For an account with cloud password enabled:

1. Receive the password only through a private owner-provided file or hidden
   terminal prompt; never place it in chat, a command argument, environment,
   docs, or logs.
2. Copy it to a mode-0600 one-time file under `/run/autostop-telegram`.
3. Start `qr-login --password-file ...`; verify the one-time copy disappears
   immediately after reading.
4. Show the current unique QR and let the owner accept it from Telegram
   Settings -> Devices.
5. After authorization, enable/start the daemon, verify status and a bounded
   dialog read, restart once, and verify authorization again.
6. Delete expired QR images. Do not delete the owner's source attachment
   without separate authorization.

If the session may be compromised, tell the owner to revoke it in an official
Telegram client immediately. Stop the bridge while revocation is pending. Do
not attempt silent session migration or export.

## Privacy And Platform Rules

- Work only with the owner's knowledge and request. Never perform background
  outreach, mass collection, bulk export, scraping, training, or profiling.
- Keep Telegram content transient. Durable Manager knowledge may contain only
  safe operating rules, capability names, health booleans, and de-identified
  lessons.
- Never expose VPN profiles, authentication links/codes, session material,
  private keys, passwords, phone numbers, or contact/message tables.
- Do not implement ghost mode or tamper with Telegram read/online/self-destruct
  semantics.
- Do not route CRM traffic through a VPN or change default route/DNS/firewall
  for Telegram. Diagnose only the existing Telegram-specific path. For work on
  the FST.KZ server, first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`.

## Health And Completion

Healthy means all of the following are true:

- systemd service is enabled and active;
- Unix socket exists with mode 0600 and service ownership;
- session exists with mode 0600 and service ownership;
- `status` returns `authorized=true`;
- bounded `dialogs` and `search` return successfully;
- no warning-or-higher service errors appear after restart.

Never use a real send as a generic health check. A send requires its own exact
owner instruction and target.

## Official References

- Telegram QR login: https://core.telegram.org/api/qr-login
- Telegram user authorization: https://core.telegram.org/api/auth
- Telegram API terms: https://core.telegram.org/api/terms
- Telethon session security: https://docs.telethon.dev/en/stable/concepts/sessions.html
- Telethon session reuse FAQ: https://docs.telethon.dev/en/stable/quick-references/faq.html
