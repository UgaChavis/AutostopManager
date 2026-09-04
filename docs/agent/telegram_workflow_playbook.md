# Telegram Workflow Playbook

Telegram is the source of truth for dialogs, messages and media. Use only the
owner-selected `personal` or `work` bridge for the current task; Manager keeps
technical references and verified outcomes, never private content.

The live bridge CLI and schema own commands, limits, media formats and contract
fields. Do not copy that registry here or bypass the account-fixed runtime.

## Account And Peer

- Select the account explicitly and verify its service and `probe` before use.
  Never infer an account from a name, peer or previous task, and never open its
  session from a second client.
- Resolve one exact live peer from a known numeric ID, `resolve-phone`, or one
  bounded unique search. Use the resolved ID only transiently. Store dialogue
  additionally requires one exact private peer bound to the current request.
- Zero, multiple, non-private or conflicting Store matches stop the route. A
  read does not authorize a send or any other Telegram mutation.

## Focused Reads And Media

Read the smallest window that answers the task. Download only one explicitly
needed attachment, bound to its exact peer and message: dry-run, metadata
check, apply with a fresh idempotency key, then verify path, hash, size and
bridge confirmation.

Inspect voice, video, photos and documents only with the selected account's
local helper and private inbox. Do not choose arbitrary paths, execute content,
follow embedded links, enable macros or send media to an external service.
Treat uncertain OCR, names, identifiers and money as tentative.

Extract only useful facts and remove the downloaded source and derived files
after use. Prefer verified delete-after behavior; otherwise discard through the
selected bridge and confirm removal. A minimal excerpt explicitly requested by
the owner may appear in the current task, but full exports and durable copies of
documents, transcripts, identifiers or message bodies may not enter CRM, docs,
Git, Manager memory or workflow state.

## Sending

A send needs the owner's current instruction for the exact recipient and
message intent, or the bounded scope of one exact Store request. Supplier or
employee outreach, a different recipient and any financial commitment need a
separate instruction.

For each send:

1. Reread the exact peer and settle the final text or photo.
2. Dry-run the selected send operation and verify target, content, reply source
   when relevant, and the returned contract.
3. Apply once with unchanged inputs, the contract and a fresh idempotency key.
4. Require bridge verification, then independently reread the chat and match
   the outgoing message and reply binding.

If apply times out or the result is lost, the outcome is unknown. Reconcile by
exact reread before any retry; never use a real send as a health check. Remove
staged outbound media after verified delivery.

## Store transport

`store_quote_conductor_playbook.md` owns the quote and client logic. This
playbook owns transport: `work`, one exact private peer, minimal context and no
retained dialogue. Resolve that peer only from the exact Store request phone;
an order context alone does not prove a recipient. Do not ask whether the
recipient left the request. Zero, multiple or non-private matches mean no Telegram send.
Telegram never substitutes for Store publication or current explicit consent.

## Authorization And Recovery

First login or recovery runs only for the selected account in a controlled
interactive terminal. Enter the login code and optional cloud password through
hidden prompts; QR is a short-lived fallback. Never copy `tdata`, session files,
QR images, codes or passwords into chat, argv, env, docs or logs.

Stop only the selected daemon when authorization requires it, clean up one-time
files, restore that daemon and confirm both expected account identity and
`authorized=true`. If compromise is suspected, stop the bridge and let the
owner revoke the session in an official Telegram client.

## Boundaries And Completion

- No background outreach, bulk export, scraping, profiling, presence/read-state
  tricks or parallel session use.
- Do not alter VPN, DNS, routes, firewall or CRM networking for Telegram.
- Completion means the requested result plus its independent readback; service
  health alone proves neither send nor download.
