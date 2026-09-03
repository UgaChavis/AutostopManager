# Telegram Workflow Playbook

Use the owner's explicitly selected Telethon bridge only for the exact current
Telegram task. Telegram remains the source of truth for dialogs, contacts,
messages and media; Manager stores only de-identified operating rules and
verification.

## Runtime And Secrets

- `personal` is `autostop-telegram.service`; `work` is the separate
  `autostop-work-telegram.service`. Each owns its own credentials, session,
  state directory, Unix socket, contracts, idempotency state and immutable
  release root.
- Every command selects `--account personal|work`; account aliases use fixed
  paths and reject manual path overrides. Never infer an account from a peer,
  title or prior task.
- Never print or persist keys, login/QR/2FA data, contract tokens, sessions,
  peer IDs, phone numbers, private message bodies or role bindings.
- Use the selected daemon for normal work. Never open its SQLite session from
  a second Telethon client; stop only that exact daemon for bounded
  authorization or diagnostics and restore it immediately.

Run commands as `autostop-telegram` and expose only task-relevant fields:

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account personal probe
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

## Send

A send requires the owner's current instruction naming the exact recipient and
message/intent. Clients, suppliers, employees, financial commitments and general
outreach always require a separate direct instruction.

For every send:

1. Reread the exact peer and freeze the final text.
2. Run `send` or `send-photo` in `dry_run`; verify exact target,
   text, reply source when used, and the returned contract.
3. Apply once with unchanged inputs, the contract token and a fresh
   idempotency key.
4. Require bridge verification, then independently reread the exact chat and
   match outgoing message ID, text and reply link when applicable.

If apply times out or its response is lost, treat the outcome as unknown. Do
not resend with a new key until an exact reread proves absence. For group
replies, bind the contract to the exact incoming message and require it still
exists, is incoming and belongs to that group.

Photo sends use one service-owned mode-0600 JPEG in the private outbox, an
explicit caption and the same contract/readback sequence. Remove the staged
copy after verified delivery. Never use a real send as a health check.

## Authorization, QR And 2FA Recovery

- Work-account first authorization uses
  `scripts/authorize-telegram-account.sh --account work` from an interactive
  controlled terminal. It asks for the phone, Desktop-delivered login code and
  optional cloud password through hidden prompts; never use chat, argv, env,
  docs or logs. Do not copy Telegram Desktop `tdata` or a session file.
- QR is a fallback only: stop the selected daemon, generate a fresh unique QR
  after the prior token expires, and keep the waiting process alive while the
  owner accepts it in Telegram Devices.
- Supply a cloud password only through a hidden prompt or one-time mode-0600
  file in the selected runtime directory.
- After authorization, remove one-time files/expired QR images, restore only
  the selected daemon, and verify `probe` reports `authorized=true`. First
  connection needs no dialog read or test message.
- If compromise is suspected, stop the bridge and have the owner revoke the
  session in an official Telegram client; never export or silently migrate it.

## Privacy And Completion

- No background outreach, bulk reads/exports, scraping, profiling, ghost mode
  or changes to Telegram presence/read semantics.
- Do not change VPN, DNS, default route, firewall or CRM networking for
  Telegram. FST.KZ work first follows `/root/.codex/CODEX_VPN_FST_ACCESS.md`.
- Healthy after first connection means: enabled/active selected service,
  private selected socket and session, `probe` authorization and no new
  warning-or-higher errors. A later read/search still needs its own exact task.
- Completion requires the exact requested result plus independent readback;
  service health alone never proves a send or download succeeded.
