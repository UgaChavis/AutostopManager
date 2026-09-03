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
- Every bridge CLI command selects `--account personal|work`; account aliases
  use fixed paths and reject manual path overrides. Never infer an account from
  a peer, title or prior task.
- The personal direct media helper requires `--account personal`. Work media
  always uses the privileged account-fixed wrapper, which rejects an account
  override. Both derive the selected private inbox and local model directory;
  do not add a manual inbox or model-path override.
- Never print or persist keys, login/QR/2FA data, contract tokens, sessions,
  peer IDs, phone numbers or private message bodies.
- Use the selected daemon for normal work. Never open its SQLite session from
  a second Telethon client; stop only that exact daemon for bounded
  authorization or diagnostics and restore it immediately.
- Run the bridge as the selected account's service user: `autostop-telegram`
  for `personal`, and `autostop-work-telegram` for `work`. The work socket is
  intentionally private to its service user; invoking `--account work` as the
  personal service user fails closed with `bridge_unavailable` and is not an
  authorization failure.

Expose only task-relevant fields:

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account personal probe

sudo -u autostop-work-telegram env PYTHONPATH=/opt/autostop-work-telegram-releases/current \
  /opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work probe
```

The live bridge CLI/schema owns the current command list, media allowlist,
limits and contract fields; this playbook must not duplicate that registry.

## Read And Download

1. Require an active service and `authorized=true`.
2. Resolve one exact live peer from a known numeric ID, `resolve-phone`, or a
   bounded search with one unique exact title/username match. Then use its
   numeric ID transiently; never keep searching by a display name. A Store
   client dialogue additionally requires `kind=private`.
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
7. Remove every downloaded or derived file after use. A successful helper
   `--delete-after` is the verified cleanup for its source; do not discard that
   already absent file again. Use `discard-download` for remaining files and as
   the failure fallback, require `removed=true` when invoked, and finally verify
   that no task file remains.

The signed download contract is bound to peer, message and media metadata.
Apply re-reads and validates that message; an idempotency key may replay only
the same download. Preserve default redaction of credential-bearing URIs.

## Voice And Video

Voice/audio transcription and short MP4 inspection are local, private and
one-file-at-a-time. Use the matching selected account, service user, release
root and venv; do not cross-substitute paths or users. The selected helper
accepts the exact downloaded file only when it is in that account's inbox.

```bash
sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_transcribe \
  --account personal --file /run/autostop-telegram/inbox/EXACT_FILE --language ru --delete-after

sudo /usr/local/sbin/autostop-work-telegram-media transcribe \
  --file /run/autostop-work-telegram/inbox/EXACT_FILE --language ru --delete-after

sudo -u autostop-telegram env PYTHONPATH=/opt/autostop-telegram-releases/current \
  /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_video_preview \
  --account personal --file /run/autostop-telegram/inbox/EXACT_FILE.mp4 --delete-after

sudo /usr/local/sbin/autostop-work-telegram-media preview \
  --file /run/autostop-work-telegram/inbox/EXACT_FILE.mp4 --delete-after
```

The work helpers run in a transient systemd sandbox with no network and with
the work session, credentials and bridge socket made inaccessible. The helpers
own and enforce current ownership, signature, codec, duration, size, model and
sandbox limits. Transcription stays local. Video inspection
uses only the generated silent storyboard; it does not transcribe a video's
audio track. `--delete-after` removes the source audio/MP4 after processing.
For a video, inspect the returned storyboard transiently and then remove the
exact remaining JPEG through selected-account `discard-download`. Photos use
the same exact download, transient private inspection and discard flow; there
is no general Telegram OCR or arbitrary-file execution path.

## Store Client Dialogue

- Use `work` only. Start from one exact live Store quote request and pass its
  current phone to `resolve-phone`, then use the returned live numeric peer. If
  the phone returns zero matches and the same current request has an exact
  `telegram_username`, run one bounded exact username search and accept only
  one private peer. A display name, old alias or similar phone is not enough;
  multiple or ambiguous matches always fail closed.
- A unique peer is only a routing match. Unless the current Store record has a
  verified Telegram binding or the owner explicitly confirms that exact peer,
  the first message asks only whether the person submitted an AutoStop parts
  request. It contains no VIN, vehicle, part, price, photo or other request
  detail. Continue or disclose request data only after an affirmative reply;
  otherwise stop and report the mismatch.
- A direct owner instruction to process that exact request may cover the
  necessary bounded clarification and follow-up with that client. It never
  covers another recipient, general outreach, supplier contact, reservation,
  discount, payment or other financial promise.
- Read only the relevant window and media. Write briefly, calmly and naturally:
  one clear question when data is missing, then the useful answer. Do not send
  internal reports, source lists, automation language or long templates.
- Keep the dialogue tied to the same request. Confirmed facts return to the
  transient Store workflow; waiting for the client is `external_wait`. Telegram
  neither changes the Store status nor proves that an offer reached the client
  cabinet.

## Send

A send requires the owner's current instruction naming the exact recipient and
message/intent, or the exact Store request under the bounded dialogue rule
above. Suppliers, employees, financial commitments and general outreach always
require a separate direct instruction.

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
  the selected daemon, run one transient `status` identity check and have the
  owner confirm the expected personal/work profile without retaining its ID or
  name, then verify `probe` reports `authorized=true`. First connection needs no
  dialog read or test message.
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
