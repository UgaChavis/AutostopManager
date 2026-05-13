# CRM Manager Data Playbook

Purpose: define what manager-facing CRM information belongs in Obsidian, what
must stay in AutoStop CRM, and how Codex should refresh operational snapshots
without creating an unsafe second CRM database.

## Source Of Truth

AutoStop CRM remains the source of truth for:

- cards, columns, tags, deadlines, attachments, and archive state;
- clients, phones, vehicles, VINs, license plates, and client links;
- repair orders, works, materials, payments, PDF exports, and statuses;
- cashboxes, raw cash journals, transaction ids, and balances;
- shared workshop files such as `Клиенты.xls`.

Obsidian is the manager-readable layer for summaries, navigation, decisions,
and safe analytics. It should help the manager understand where to look and
what to improve, not replace live CRM reads.

## What To Store In Obsidian

Safe by default:

- board count snapshots: active cards, archived cards, column load, attention
  cards by short id, and workflow blockers;
- cashbox overview snapshots: box names, balances, total income/expense, last
  transaction date, and reconciliation questions;
- repair-order overview snapshots: open/ready/closed counts, unpaid totals,
  inconsistent order count, and operational risks;
- client-quality summaries: total clients, duplicate-key counts, profiles
  missing structured names, profiles without visits, and cleanup rules;
- shared-file metadata: file names, extensions, size, creation/update dates,
  and where the live source can be downloaded from CRM;
- durable conclusions, e.g. "client duplicate cleanup should start from exact
  phone matches and matching vehicle/VIN evidence."

Do not store in cloud Obsidian without an explicit owner request for that exact
export:

- full client phone lists;
- full VIN/license plate/client-name databases;
- raw cashbox transaction ledgers;
- full repair-order text dumps;
- full board exports or raw card descriptions;
- secrets, bearer tokens, OAuth state, env files, or supplier credentials.

If the owner asks for a full data export, put it in a clearly named private
folder first and confirm whether it may be synced to Google Drive. Prefer CRM
or a local private file under `data/private_knowledge/` for raw personal data.

## Refresh Workflow

Use the live AutoStop CRM MCP connector, not stale local files, for operational
snapshots:

1. `bootstrap_context` - verify board identity, scope, active/archive counts,
   columns, and attention cards.
2. `list_clients(include_stats=true)` - gather compact client totals and
   quality signals. Do not copy full phone rows into Obsidian.
3. `list_cashboxes` - gather balances and transaction counts.
4. `list_repair_orders(status=all)` - gather status counts, unpaid/due totals,
   and inconsistent order count.
5. `list_shared_files` - record metadata for files such as `Клиенты.xls`.
6. Write or update dated Obsidian snapshots under `40_Operations/`.
7. Link the snapshot from `10_Manager/CRM/CRM manager workspace.md`.

For exact client/card/order work, read back the live target with
`get_client`, `get_card_context`, `get_repair_order`, or `get_cashbox`.

## Read-Only CRM Health Flow

Use this flow before proposing CRM hygiene work. It is read-only and must not
write to CRM, move cards, archive cards, or edit Obsidian snapshots unless the
owner gives a separate explicit owner command.

1. Call `bootstrap_context` to confirm board identity and connector health.
2. Call `get_board_context` or `review_board` to get active counts, overloaded
   columns, stale/attention cards, and recent event noise.
3. Flag overloaded columns before card-level work. Current known risk signals
   to watch are `Запись на ремонт` and `Готовые автомобили`.
4. For focused targets, use `search_cards` and `get_card_context`; check for
   missing next action, stale or missing `board_summary`, missing deadline,
   unclear payment/parts state, or unclear repair-order closure state.
5. If money, works, materials, ready state, or closure matters, read
   `get_repair_order`; if payment evidence matters, read `get_cashbox` or the
   relevant cash journal.
6. Produce proposed actions only: summary refresh, tag/indicator/deadline
   suggestion, missing-data question, archive recommendation, or owner decision
   blocker.
7. Report `cards_moved=0` and `cards_archived=0` for this flow.

Recent QA/test-card events can pollute board history. Treat them as connector
health evidence, not as live customer-work priority signals.

## Obsidian Targets

Use these notes as the manager workspace:

- `10_Manager/CRM/CRM manager workspace.md` - daily entrypoint for CRM manager
  context and links to current snapshots.
- `40_Operations/Snapshots/CRM board snapshot YYYY-MM-DD.md` - board load and
  attention summary.
- `40_Operations/Finance/Cashboxes overview YYYY-MM-DD.md` - cashbox overview.
- `40_Operations/Repair Orders/Repair orders overview YYYY-MM-DD.md` - repair
  order health summary.
- `40_Operations/Clients/Clients overview YYYY-MM-DD.md` - client-data quality
  summary.

## Manager Judgement Rules

- Use Obsidian to remember patterns and next checks; use CRM to modify state.
- Treat duplicate-client findings as leads, not facts, until matching phone,
  vehicle, VIN/frame, or card history is confirmed.
- Never create, link, merge, delete, or overwrite clients based only on an
  Obsidian snapshot.
- For cashboxes, snapshots can show totals and risks, but transaction-level
  reconciliation must happen from CRM cashbox tools.
- For ready/unpaid vehicles, use Obsidian only for overview. The action target
  is always the live CRM card or repair order.
