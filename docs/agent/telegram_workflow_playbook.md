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
- Immutable code release: `/opt/autostop-telegram-releases/current`; Telegram
  releases are deployed independently and do not switch the CRM Manager
  release.

Never print, copy, attach, back up to Git, or persist credential values, QR
tokens, 2FA, login codes, contract tokens, or the session file. A Telethon
session contains the authorization key and can grant account access.

## Fast Start

Run commands as `autostop-telegram`; return only bounded, task-relevant fields.

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge status
```

Current read surface:

- `status`
- `dialogs --limit N`
- `search --query TEXT --limit N`
- `read --peer ID --limit N`
- `download --peer ID --message-id ID --mode dry_run`
- `download --peer ID --message-id ID --mode apply --contract-token TOKEN --idempotency-key KEY`
- `discard-download --file /run/autostop-telegram/inbox/NAME`

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
6. `read` returns bounded media metadata for each attachment: original basename,
   Telegram media type, MIME type, byte size, supported suffix and whether the
   bridge can download it.
7. When the owner's task requires the attachment, resolve one exact `message id`,
   run `download` dry-run, verify the metadata, then apply with the unchanged
   target/message contract and a fresh idempotency key.
8. Summarize private content. Do not paste full threads, large channel feeds,
   contact tables, or message exports into chat or project files.

`read` redacts `vpn://` and `tg://` credential-bearing URIs by default. Keep
that redaction in any derived output. Do not treat reading as permission to
send, forward, delete, edit, join, leave, block, or change read state. Download
media only when it is necessary to the owner's current task; ordinary dialog
reading must not download attachments.

## Attachment Workflow

The bridge accepts one exact incoming or outgoing Telegram message containing
a supported attachment. It never accepts a destination path from the caller.
The service writes only to its mode-0700 `/run/autostop-telegram/inbox`
directory and creates mode-0600 files named from the message ID and content
hash. Current limit is 25 MiB. Supported formats are JPEG, PNG, WebP, PDF,
DOCX, XLSX, UTF-8 TXT, UTF-8 CSV, Telegram Voice OGG/Opus, MP3 and M4A.
Audio is limited to 10 minutes. Archives, executables, scripts, videos and
unknown MIME types fail closed.

1. Check `status`, resolve the exact peer and run a bounded `read`.
2. Select one exact message ID whose `media.downloadable` is true.
3. Run `download --mode dry_run`; verify peer, message ID, MIME, suffix and
   byte size. Never print or persist the contract token.
4. Run `download --mode apply` with the same peer/message, returned token and a
   fresh idempotency key. Verify `verified=true`, SHA-256, size and that the
   returned path is below `/run/autostop-telegram/inbox`.
5. Recognize without executing content:
   - JPEG/PNG/WebP: inspect with the local image viewer; OCR only the needed
     fields and treat OCR as tentative when unclear.
   - PDF: render or use `pdftotext`; never follow embedded links or launch
     attachments.
   - DOCX/XLSX: extract text/cells with a non-macro data reader or isolated
     headless conversion; never enable macros.
   - TXT/CSV: read bounded UTF-8 content.
   - Telegram Voice/audio: run the private local transcription workflow below.
6. Use only task-relevant facts. Do not paste full private documents into chat,
   CRM descriptions, docs, Git, memory or workflow state.
7. Run `discard-download --file <exact returned path>` after the task. Confirm
   `removed=true`. If analysis fails, discard the exact file before reporting.

`download` uses a signed 15-minute dry-run contract bound to peer ID, message
ID and media metadata. Apply re-reads that exact message, checks the Telegram
size, validates file signatures, hashes the bytes and records only a private
idempotency receipt. A repeated key may only replay the exact same download.

## Voice Message Workflow

Voice recognition is local and on demand. It uses the pinned Faster Whisper
`small` model from `/var/lib/autostop-telegram/models/faster-whisper-small` on
CPU with INT8 computation. The installed `Systran/faster-whisper-small` model
revision is `536b0662742c02347bc0e980a01041f333bce120`; inference is
`local_files_only` and does not contact the model provider. The bridge does not
call an external transcription API and never stores transcript text in Manager
state.

1. Resolve the exact peer and bounded dialog window, then select one message
   whose media metadata has `voice=true` or a supported audio MIME type.
2. Run the normal `download` dry-run/apply flow for that exact message.
3. Verify the returned path, MIME, SHA-256 and size.
4. Run as the service account:

   ```bash
   sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
     /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_transcribe \
     --file /run/autostop-telegram/inbox/EXACT_FILE.ogg --language ru --delete-after
   ```

5. The transcriber verifies private ownership/mode, uses `ffprobe` to require
   exactly one audio stream without video and rejects files above 10 minutes or
   25 MiB. It loads the model only from the private local model directory.
6. Summarize or act only on task-relevant facts. Treat unclear names, numbers,
   plates, VINs, part numbers and money as tentative until confirmed.
7. `--delete-after` removes the exact audio file after success or failure. If
   the command is interrupted before cleanup, use `discard-download` on the
   exact returned path and verify `removed=true`.

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
