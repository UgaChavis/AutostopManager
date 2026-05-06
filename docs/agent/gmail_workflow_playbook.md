# Gmail Workflow Playbook

Purpose: make Gmail work predictable, private, and fast for AutostopManager.
Gmail remains the source of truth for messages, threads, drafts, attachments,
labels, and sent history. Manager memory stores only durable conclusions.

## Start Here

1. Use `mcp__codex_apps__gmail._list_labels` for counts and user-label IDs.
2. Use `mcp__codex_apps__gmail._search_emails` for normal searches and triage
   summaries.
3. Use `mcp__codex_apps__gmail._search_email_ids` only when exact IDs are needed
   for batch reads or an owner-approved mutation.
4. Read the exact message or thread before drafting, forwarding, tasking,
   reminding, or saving facts to memory.
5. Summarize private content. Do not copy raw full bodies into chat reports,
   project docs, or manager memory.

## Read-Only Tool Order

Use these commands freely when the task requires Gmail inspection:

- `_list_labels` - label inventory, label IDs, unread/inbox/draft counts.
- `_search_emails` - primary search with snippets, labels, attachment metadata,
  display URL, and pagination.
- `_search_email_ids` - compact ID list for follow-up reads.
- `_batch_read_email` - read several known messages.
- `_read_email_thread` - read one conversation from a message ID or thread ID.
- `_batch_read_email_threads` - read several conversations for triage.
- `_list_drafts` - inspect draft summaries without changing them.
- `_read_attachment` - fetch an attachment when the attachment itself matters.

## Write Safety

These commands change Gmail or send mail and require an explicit owner
instruction for the exact action:

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

Before running a mutating command, identify the exact target messages, labels,
recipients, subject, and intended result. For server-side bulk labeling, preview
the Gmail query with `_search_emails` first and report the query back before
executing.

## Query Patterns

Prefer narrow Gmail queries:

```text
in:inbox newer_than:30d
from:example@example.com newer_than:90d
subject:(счет OR акт) has:attachment newer_than:180d
label:Работа newer_than:90d
has:attachment newer_than:90d filename:pdf
older_than:1y from:newsletter@example.com
```

For user-label work:

1. Run `_list_labels`.
2. Capture both display name and label ID.
3. Use `tags` with label IDs when the tool needs exact label filtering.
4. Use `label:<name>` in Gmail query only for normal search readability.

## Attachment Rules

Read attachment metadata from `_search_emails` first. Use `_read_attachment`
only when the file is needed. For invoices, acts, КП, requisites sheets, PDF,
Word, or Excel documents, route the fetched file through the business-document
quality gate before relying on layout, totals, or legal wording.

Known caveat from the 2026-05-05 audit: `_read_attachment` returned a PDF file
successfully, but parsed text was partly garbled. Treat connector-parsed PDF
text as a convenience preview, not as final OCR or layout evidence.

## Decoding And Noise

Some supplier or Russian messages can return garbled body text while the
subject and snippet remain readable. When this happens:

- do not make a hard conclusion from the garbled body;
- use the snippet, sender, subject, timestamp, labels, and attachments;
- search the same thread or read related messages;
- if the exact content matters, ask to inspect the message in Gmail or use an
  exported attachment/source file.

Newsletter bodies can be very large and tracking-heavy. Summarize only what the
owner asked for.

## Memory Extraction

Store only durable email-derived facts:

- decisions and approvals;
- deadlines and follow-ups;
- supplier/client commitments;
- payment or domain/hosting risks;
- reusable owner preferences and operating rules.

Do not store:

- full private threads;
- raw message bodies;
- temporary promotions and newsletters;
- attachment contents;
- one-off search results that are easy to re-query from Gmail.

## Audit Result 2026-05-05

Read-only commands tested successfully:

- labels and counts;
- primary search;
- ID search;
- tags filtering;
- pagination;
- draft listing;
- batch message reads;
- single-thread read;
- batch thread read;
- PDF attachment fetch.

Mutating and sending commands were intentionally not executed. Their schemas are
available, but they require explicit owner approval and a final target review.
