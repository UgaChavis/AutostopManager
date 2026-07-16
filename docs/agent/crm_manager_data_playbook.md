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

1. `agent_bootstrap` - verify board identity, scope, active/archive counts,
   columns, and policy state.
2. `agent_board_digest` - gather the compact active or archived card view.
3. `agent_board_workflow(operation="manager_board_scan", mode="dry_run")` -
   gather inbox, ready unpaid, missing-data, timer, and ЗН consistency signals.
4. `agent_search` - gather focused client, repair-order, cashbox, inventory, or
   file results without copying private rows into chat.
5. `agent_entity_context` - read one exact client, card, order, cashbox,
   inventory item, or file when detail is required.
6. Return a compact report in chat, a Gateway v2 workflow checkpoint, or a
   schema-hashed raw `manager_journal` entry only when the result is durable.

For exact client/card/order work, read back the live target with
`agent_entity_context`.

For any write, continue with `prepare_action_contract`, run the applicable
named workflow in `dry_run` and then `apply`, and reread the exact target. Do
not invoke a hidden legacy capability directly; use guarded raw discovery only
when no named workflow exists.

## Read-Only CRM Health Flow

Use this flow before proposing CRM hygiene work. It is read-only and must not
write to CRM, move cards, archive cards, or edit runtime files unless the owner
gives a separate explicit command.

1. Call `agent_bootstrap` to confirm board identity and connector health.
2. Call `agent_board_workflow(operation="manager_board_scan", mode="dry_run")`
   to get active counts, overloaded columns,
   stale/attention cards, ready unpaid cards, inbox cards, and repair-order
   consistency issues.
3. Use `agent_board_digest` when the high-level scan is not enough.
4. Flag overloaded columns returned by the current scan before card-level work;
   do not hard-code transient load signals into durable documentation.
5. For focused targets, use `agent_search` and `agent_entity_context`; check for
   missing next action, stale or missing `board_summary`, missing deadline,
   unclear payment/parts state, or unclear repair-order closure state.
6. If money, works, materials, ready state, closure, or payment evidence
   matters, use `agent_entity_context` or the finance workflow.
7. Produce proposed actions only: summary refresh, tag/indicator/deadline
   suggestion, missing-data question, archive recommendation, or owner decision
   blocker.
8. Report `cards_moved=0` and `cards_archived=0` for this flow.

For safe bulk writes, use high-level operations first:

- timer floor: `agent_board_workflow(operation="bulk_set_deadline_if_below")`,
  dry-run then apply;
- missing/stale previews:
  `agent_board_workflow(operation="bulk_refresh_board_summaries")`, dry-run then apply;
- ready unpaid follow-up:
  `agent_board_workflow(operation="apply_ready_unpaid_followups")`, dry-run then apply;
- one-card cleanup: `agent_board_workflow(operation="cleanup_card")`, dry-run then apply.

### Active-card timer floor

For an owner request such as "всем активным карточкам таймер более двух суток":

1. Build `prepare_action_contract` with `domain="board"`,
   `action="bulk_set_deadline_if_below"`, `target_id="active_cards"`, and
   `planned_changes={"include_archived": false, "min_total_seconds": 172800,
   "target_total_seconds": 173700}`. This is a collection-scoped action, so it
   does not require a synthetic board `expected_revision`.
2. Call `agent_board_workflow(operation="bulk_set_deadline_if_below",
   mode="dry_run")` with the same payload and a dry-run idempotency key.
3. If verification passes, call the same named workflow with `mode="apply"`
   and a new apply idempotency key.
4. Confirm the apply workflow reports no active cards below `172800`, then
   reread the active digest or live state and confirm archived cards were not
   changed.

`agent_board_workflow` owns and closes its Gateway v2 ledger automatically.
Do not create a separate manual `start_workflow` for this single named
operation. Dry-run and apply intentionally receive separate run ids; their
responses and stored workflow metadata must state `mode`/`dry_run` explicitly.
Use a parent manual workflow only when one owner request genuinely combines
several named operations or external systems.

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
