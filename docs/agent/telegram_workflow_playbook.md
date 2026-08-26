# Telegram Workflow Playbook

Use the owner's private Telethon bridge only for the exact current Telegram
task. Telegram remains the source of truth for dialogs, contacts, messages and
media; Manager stores only de-identified operating rules and verification.

## Runtime And Secrets

- Service: `autostop-telegram.service`; canonical socket:
  `/run/autostop-telegram/bridge.sock` with mode 0600 and service ownership.
- Use `/opt/autostop-telegram-venv` and the active immutable release at
  `/opt/autostop-telegram-releases/current`.
- Credentials and the Telethon session stay in service-controlled files under
  `/etc/autostop-telegram` and `/var/lib/autostop-telegram`.
- Never print or persist keys, login/QR/2FA data, contract tokens, sessions,
  peer IDs, phone numbers, private message bodies or role bindings.
- Use the daemon for normal work. Never open its SQLite session from a second
  Telethon client; stop it only for bounded authorization or diagnostics and
  restore it immediately.

Run commands as `autostop-telegram` and expose only task-relevant fields:

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge status
```

The live bridge CLI/schema owns the current command list, media allowlist,
limits and contract fields; this playbook must not duplicate that registry.

## Read And Download

1. Require an active service and `authorized=true`.
2. Resolve one exact live peer through a verified role or bounded search. Once
   resolved, use its exact numeric ID transiently; never keep searching by a
   display name.
3. Read the smallest useful window, normally 3-20 messages. Reading does not
   authorize sending, forwarding, editing, deleting, joining or changing read
   state.
4. Download only an attachment needed for the current task: exact peer and
   message, `dry_run`, metadata check, `apply` with the unchanged contract and
   a fresh idempotency key, then verify path, hash, size and `verified=true`.
5. Accept only the bridge's current supported formats and private inbox path.
   Never choose a destination, follow embedded links, enable macros, execute
   content or send media to an external service.
6. Extract only needed facts and treat uncertain OCR, names, identifiers and
   money as tentative. Keep full documents and transcripts out of chat, CRM,
   docs, Git, memory and workflow state.
7. Run `discard-download` for every exact downloaded or derived file after use,
   including on failure, and require `removed=true`.

The signed download contract is bound to peer, message and media metadata.
Apply re-reads and validates that message; an idempotency key may replay only
the same download. Preserve default redaction of credential-bearing URIs.

## Voice And Video

Voice/audio transcription and short MP4 inspection are local, private and
one-file-at-a-time. Use the downloaded exact path with:

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_transcribe \
  --file /run/autostop-telegram/inbox/EXACT_FILE --language ru --delete-after

sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_video_preview \
  --file /run/autostop-telegram/inbox/EXACT_FILE.mp4 --delete-after
```

The helpers own and enforce current ownership, signature, codec, duration,
size, model and sandbox limits. Transcription stays local. Video inspection
uses only the generated silent storyboard; discard any surviving original and
preview paths.

## Send And Roles

A send requires the owner's current instruction naming the exact recipient and
message/intent. An active director goal permits only allowlisted operational
questions to these bound private roles:

- `director_admin` — main administrator;
- `director_reception` — current reception employee;
- `director_workshop` — workshop foreman.

Role labels are identity hints, never targets. `roles` must report the selected
binding as `bound=true`, `verified=true`, `kind=private`; drift, ambiguity or a
missing contact fails closed. Binding or replacement requires its own exact
dry-run/apply/readback. Clients, suppliers, other employees, financial
commitments and general outreach always require a separate direct instruction.

For every send:

1. Reread the exact peer/role and freeze the final text.
2. Run `send`, `send-role` or `send-photo` in `dry_run`; verify exact target,
   text, reply source when used, and the returned contract.
3. Apply once with unchanged inputs, the contract token and a fresh
   idempotency key.
4. Require bridge verification, then independently reread the exact chat and
   match outgoing message ID, text and reply link when applicable.

If apply times out or its response is lost, treat the outcome as unknown. Do
not resend with a new key until an exact reread proves absence. For group
replies, bind the contract to the exact incoming message and require it still
exists, is incoming and belongs to that group.

Director follow-ups use a refs-only `workflow_wait_for_external` step: IDs,
timestamps, safe purpose hash and next-check time only. Read a small later
window, accept only a relevant newer reply and avoid polling; one reminder is
the default maximum.

Photo sends use one service-owned mode-0600 JPEG in the private outbox, an
explicit caption and the same contract/readback sequence. Remove the staged
copy after verified delivery. Never use a real send as a health check.

## QR And 2FA Recovery

- Stop the daemon for the bounded authorization session.
- Supply a cloud password only through a hidden prompt or one-time mode-0600
  file under `/run/autostop-telegram`; never use chat, argv, env, docs or logs.
- Generate a fresh unique QR only after the previous token expires and keep the
  waiting process alive while the owner accepts it in Telegram Devices.
- After authorization, remove one-time files/expired QR images, restore the
  daemon, verify status and a bounded read, restart once and verify again.
- If compromise is suspected, stop the bridge and have the owner revoke the
  session in an official Telegram client; never export or silently migrate it.

## Privacy And Completion

- No background outreach, bulk reads/exports, scraping, profiling, ghost mode
  or changes to Telegram presence/read semantics.
- Do not change VPN, DNS, default route, firewall or CRM networking for
  Telegram. FST.KZ work first follows `/root/.codex/CODEX_VPN_FST_ACCESS.md`.
- Healthy means: enabled/active service, private socket and session, authorized
  status, successful bounded read/search and no new warning-or-higher errors.
- Completion requires the exact requested result plus independent readback;
  service health alone never proves a send or download succeeded.
