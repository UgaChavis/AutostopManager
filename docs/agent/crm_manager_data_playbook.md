# CRM Manager Data Playbook

Purpose: define how manager-facing CRM summaries may be produced without
creating a second CRM data store.

## Source Of Truth

AutoStop CRM remains the source of truth for:

- cards, columns, tags, deadlines, attachments, and archive state;
- clients, phones, vehicles, VINs, license plates, and client links;
- repair orders, works, materials, payments, PDF exports, and statuses;
- cashboxes, raw cash journals, transaction ids, and balances;
- shared workshop files such as `Клиенты.xls`.

AutostopManager may keep short durable conclusions, rules, tasks, reminders,
and operating lessons. It must not mirror live CRM tables or raw records.

## Safe Summary Output

Safe by default:

- board load summaries: active card count, archived card count, column load,
  attention cards by short id, and workflow blockers;
- cashbox overviews: box names, balances, total income/expense, last
  transaction date, and reconciliation questions;
- repair-order overviews: open/ready/closed counts, unpaid totals,
  inconsistent order count, and operational risks;
- client-quality signals: total clients, duplicate-key counts, profiles
  missing structured names, profiles without visits, and cleanup rules;
- shared-file metadata: file names, extensions, size, creation/update dates,
  and where the live source can be downloaded from CRM;
- durable conclusions such as "client duplicate cleanup should start from exact
  phone matches and matching vehicle/VIN evidence."

Do not store:

- full client phone lists;
- full VIN/license plate/client-name tables;
- raw cashbox transaction ledgers;
- full repair-order text dumps;
- full board exports or raw card descriptions;
- secrets, bearer tokens, OAuth state, env files, or supplier credentials.

If the owner asks for a raw data export, keep it as a local private runtime
artifact under `data/` unless the owner gives a separate exact destination and
privacy instruction.

## Refresh Workflow

Use the live AutoStop CRM MCP connector, not stale local files, for operational
summaries:

1. `bootstrap_context` - verify board identity, scope, active/archive counts,
   columns, and attention cards.
2. `manager_board_scan` - gather compact active/archived counts, inbox, ready
   unpaid cards, missing manager data, timer risks, and ЗН consistency signals.
3. `list_clients(include_stats=true)` - gather compact client totals and
   quality signals. Do not copy full phone rows.
4. `list_cashboxes` - gather balances and transaction counts.
5. `list_repair_orders(status=all, compact=true, redact_private=true)` - gather
   status counts, unpaid/due totals, and inconsistent order count.
6. `list_shared_files` - record metadata for files such as `Клиенты.xls`.
7. Return a compact report in chat, a manager run ledger event, or a short
   `manager_journal` entry only when the result is durable.

For exact client/card/order work, read back the live target with `get_client`,
`get_card_context`, `get_repair_order`, or `get_cashbox`.

## Read-Only CRM Health Flow

Use this flow before proposing CRM hygiene work. It is read-only and must not
write to CRM, move cards, archive cards, or edit runtime files unless the owner
gives a separate explicit command.

1. Call `bootstrap_context` to confirm board identity and connector health.
2. Call `manager_board_scan` to get active counts, overloaded columns,
   stale/attention cards, ready unpaid cards, inbox cards, and repair-order
   consistency issues.
3. Use `get_board_context` or `review_board` only when the high-level scan is
   not enough.
4. Flag overloaded columns before card-level work. Current known risk signals
   to watch are `Запись на ремонт` and `Готовые автомобили`.
5. For focused targets, use `search_cards` and `get_card_context`; check for
   missing next action, stale or missing `board_summary`, missing deadline,
   unclear payment/parts state, or unclear repair-order closure state.
6. If money, works, materials, ready state, or closure matters, read
   `get_repair_order`; if payment evidence matters, read `get_cashbox` or the
   relevant cash journal.
7. Produce proposed actions only: summary refresh, tag/indicator/deadline
   suggestion, missing-data question, archive recommendation, or owner decision
   blocker.
8. Report `cards_moved=0` and `cards_archived=0` for this flow.

For safe bulk writes, use high-level operations first:

- timer floor: `bulk_set_deadline_if_below(mode=dry_run)` then `apply`;
- missing/stale previews: `bulk_refresh_board_summaries(mode=dry_run)` then
  `apply`;
- ready unpaid follow-up: `apply_ready_unpaid_followups(mode=dry_run)` then
  `apply`;
- one-card cleanup: `cleanup_card(mode=dry_run)` then `apply`.

Recent QA/test-card events can pollute board history. Treat them as connector
health evidence, not as live customer-work priority signals.

## Manager Judgement Rules

- Use CRM reads for exact state and AutostopManager only for durable guidance.
- Treat duplicate-client findings as leads, not facts, until matching phone,
  vehicle, VIN/frame, or card history is confirmed.
- Never create, link, merge, delete, or overwrite clients based only on a
  summary.
- For cashboxes, summaries can show totals and risks, but transaction-level
  reconciliation must happen from CRM cashbox tools.
- For ready/unpaid vehicles, the action target is always the live CRM card or
  repair order.
