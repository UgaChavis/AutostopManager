# Gmail Workflow Playbook

Purpose: make Gmail work predictable, private, and fast for AutostopManager.
Gmail remains the source of truth for messages, threads, labels, drafts,
attachments, and sent history. Manager memory stores only durable conclusions.

## Start Here

Connector tool names can vary by active surface. Prefer the currently exposed
Gmail connector tools and never invent message, thread, draft, label, or
attachment IDs.

1. Use `_list_labels` for counts and user-label IDs.
2. Use `_search_emails` for normal search and triage summaries.
3. Use `_search_email_ids` only when exact message IDs are needed for batch
   reads or an owner-approved mutation.
4. Read the exact message/thread before drafting, forwarding, tasking,
   reminding, or saving email-derived facts.
5. Summarize private content; do not copy raw full bodies into chat reports,
   project docs, or manager memory.

## Current Tool Surface

Read-only or inspection tools:

- `_get_profile`
- `_list_labels`
- `_search_emails`
- `_search_email_ids`
- `_read_email`
- `_batch_read_email`
- `_read_email_thread`
- `_batch_read_email_threads`
- `_list_drafts`
- `_read_attachment`

Mutating or sending tools, all requiring task-specific owner intent and exact
targets in the active workflow:

- `_create_label`
- `_apply_labels_to_emails`
- `_batch_modify_email`
- `_bulk_label_matching_emails`
- `_archive_emails`
- `_delete_emails`
- `_create_draft`
- `_update_draft`
- `_send_draft`
- `_send_email`
- `_forward_emails`

## Autonomous Voice Execution And Write Safety

For a direct owner voice command, resolve the operational details yourself.
The command authorizes a focused read, classification, and a homogeneous
selection that reasonably achieves the named outcome. Do not ask the owner to
provide a date range, query, IDs, labels, or a choice among safe ways to carry
out a routine mailbox task. Infer them from the current mailbox and report the
completed result after verification.

For example, «вычисти технический мусор» authorizes finding and processing the
currently relevant automated alerts, CI/build notifications, routine test
reports, and newsletters as a separate homogeneous class. «Удали уведомления
GitHub о падениях» authorizes a bounded Trash operation over that identified
class, even if the owner did not state each message ID or date. Preserve a
message when its sender, subject, thread context, or attachments indicate an
account or security alert, client, supplier, bank, government body, payment,
contract, legal obligation, or a non-technical personal matter.

Before any mutating Gmail command, resolve the exact action and resulting
message IDs or query from that authorized scope. Agent Gateway v2 has no second
owner-confirmation state once that task-specific intent and target are present:
use automatic preflight, an idempotency key, active tool schema inspection, and
result readback. Ask only when the business purpose conflicts, a target is
genuinely ambiguous, the action would irreversibly affect a mixed set that may
contain significant mail, or an external send has an ambiguous recipient.

- For individual changes, use message IDs returned by Gmail search/read tools.
- For server-side bulk labeling, archiving, or Trash, preview the Gmail query
  with `_search_emails` first, classify the result, then execute immediately
  when it is the homogeneous class authorized by the owner. The preview is an
  internal verification step, not a reason to request confirmation or issue an
  intermediate report.
- Prefer archive when the owner asks generally to clean or hide routine noise.
  Move an explicitly named homogeneous class to Trash when the owner says
  «удали» or equivalent; Trash remains recoverable. Never treat a vague cleanup
  command as authority to delete a mixed or business-significant selection.
- For sends/forwards, resolve and verify recipients, subject, body, attachment
  paths, and whether the message is a new email, reply, draft, or forward.
- For CRM+Gmail workflows, store only connector, action, message/thread/draft/
  attachment/file IDs, timestamps, and status in manager SQLite. Never store
  the raw body, HTML, snippet, or full subject there.

Attachment/body schema note: current exposed create/send metadata accepts
`attachment_files` as an array of absolute local paths and also supports
`body_file`, `html_body`, and `content_type`. Inspect the active schema
immediately before sending, pass paths in its exact shape, and never pass base64
content.

## Query Patterns

Prefer narrow Gmail queries:

```text
in:inbox newer_than:30d
from:example@example.com newer_than:90d
subject:(счет OR акт) has:attachment newer_than:180d
has:attachment newer_than:90d filename:pdf
older_than:1y from:newsletter@example.com
```

For label work:

1. Run `_list_labels`.
2. Capture both display name and label ID.
3. Use label IDs where a tool expects IDs.
4. Use `label:<display name>` only in normal Gmail search queries.

## Attachments

Read attachment metadata from search/read results first. Use `_read_attachment`
only when the file itself matters. For invoices, acts, КП, requisites sheets,
PDF, Word, or Excel, route fetched files through
`business_document_quality_playbook.md` before relying on layout, totals, or
legal wording.

Known caveat: connector-parsed PDF text can be garbled. Treat it as a preview,
not as render or OCR evidence.

## Decoding And Noise

Supplier or Russian messages may return garbled bodies while subject/snippet
metadata remains usable. In that case:

- do not make a hard conclusion from garbled body text;
- use snippet, sender, subject, timestamp, labels, and attachments;
- search/read the related thread;
- if exact wording matters, inspect Gmail or the exported attachment.

Newsletter bodies can be large and tracking-heavy. Summarize only what the
owner asked for.

## Memory Extraction

Store only durable email-derived facts:

- decisions and approvals;
- deadlines and follow-ups;
- supplier/client commitments;
- payment, domain, or hosting risks;
- reusable owner preferences and operating rules.

Do not store raw private threads, full message bodies, temporary promotions,
attachment contents, or one-off search results that are easy to re-query from
Gmail.

## Verification Record

This playbook was refreshed on 2026-07-19 from the current connector schemas.
The Manager ActionContract covers send, forward, label, archive, Trash,
batch-modify, bulk-label, label creation, and draft create/update/send,
including connector-style action aliases. A self-addressed live smoke verified
profile/labels/search, draft create/update/send, message readback, label
add/remove, archive, and Trash cleanup. Only booleans and freshness are retained
in the root-only Gmail proof; no subject, body, address, or message/thread ID is
stored in project files or Manager memory.
