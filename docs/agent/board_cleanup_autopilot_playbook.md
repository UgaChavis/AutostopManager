# Board Cleanup Autopilot Playbook

Canonical behavior for the exact owner command `Приберись`. Do not document
aliases. This playbook is the only detailed source of truth; route maps,
catalogs and rules point here instead of copying the procedure.

## Authority And Boundaries

`Приберись` cleans one named card, an explicit group, or eligible active-board
cards. It may read the focused card/client/vehicle/order context and update:

- confirmed title and compact vehicle;
- a public description, including creating a minimum-sufficient description
  from confirmed working facts when an empty card needs a human handoff;
- up to three operational tags;
- hidden `board_summary`;
- source-backed vehicle profile and clear client/vehicle links;
- one short factual `AI:` note when it adds a real question or conclusion.

It may also perform a safe task already written in the card: VIN/frame decode,
OEM/analog lookup, parts availability, or a small maintenance quote.

It must not move, archive or delete cards; change deadlines/indicators; edit
repair-order rows, prices or totals; or change payments/cashboxes without a
separate explicit owner command for that exact target.

## Scope And Selection

- `Приберись в карточке <id/name>`: one exact card.
- `Приберись во входящих/готовых/по этим карточкам`: that bounded group.
- Plain `Приберись`: eligible active cards, highest-risk first.

For a multi-card run, start/resume a Gateway v2 workflow, read
`agent_bootstrap`, `agent_board_digest` and `manager_board_scan` dry-run, then
prioritize critical/inbox/ready-unpaid cards, missing or stale manager data,
payment/parts blockers and repair-order inconsistencies. Process 10-15 cards
per verified batch unless the owner asks to continue; skip already-clear cards.

Checkpoint scope, candidates, write/skip batches and final verification with
the current `state_version`. Keep board dumps, contacts, VIN/plate tables and
full orders out of chat and durable memory. Resume an unfinished run through
bootstrap, `workflow_status` and `workflow_resume`.

## Read And Write Flow

1. Resolve the exact card and current revision through focused CRM reads.
2. Read client/order/log context only when it explains identity, money,
   completion, contradiction or a blocker.
3. Build `prepare_action_contract` for the smallest confirmed patch.
4. Call `agent_board_workflow(operation="cleanup_card")` in `dry_run`, then
   `apply`, with `expected_updated_at` and a unique idempotency key.
5. For structured vehicle data use payload key `vehicle_profile`, never
   `vehicle_profile_patch`, and send only fields to merge.
6. Reread every planned field. Verify public text, vehicle metadata,
   `board_summary_stale=false`, and absence of unplanned changes; a successful
   workflow envelope alone is insufficient.

## Preservation And Structured Fields

Preserve staff-entered evidence: order works/materials/prices/totals,
payments, files, historical notes, contacts, identifiers and manual diagnosis.
Fix only supported facts; when sources disagree, retain the original and keep
uncertainty in the internal owner report.

Clean structured identity before public text:

- phone goes to the client;
- VIN/plate/mileage and aggregates go to the vehicle passport;
- engine, gearbox and drivetrain require card/order/attachment/VIN evidence;
- manual profile fields remain authoritative;
- match clients by exact phone first and never force, merge or duplicate an
  ambiguous client.

After verified transfer, do not repeat private structured fields in public text
unless they are still operationally necessary.

## Description, Summary, Title And Tags

Public description follows
`docs/agent/crm_card_description_standard.md`; do not restate its formatting,
content or empty-description rules here. `board_summary` stays separate and
contains only a short plain operator preview, without rich formatting, private
identity, raw scans or source lists.

Description quality is judged by operational completeness, not minimum length.
For a nontrivial active card, preserve every applicable confirmed complaint,
finding, agreed work, parts state, result and customer arrangement; do not
collapse that handoff into a three-word task label. Create text for an empty
description only from supported facts, and leave it empty when no useful facts
are available.

- keep `vehicle` as a compact make/model;
- keep `title` as the short issue/work/result essence;
- use no more than three tags and retain useful existing tags;
- tags are operator action cues, not decoration;
- do not rewrite a clear and operationally complete card only for style.

Typical cues: `НУЖЕН VIN`, `ЖДЕТ ЗАПЧАСТИ`, `ЗАПЧАСТИ ПРИШЛИ`,
`СОГЛАСОВАТЬ`, `ЖДЕТ ОПЛАТЫ`, `НУЖНА ДИАГНОСТИКА`, `ЖДЕМ КЛИЕНТА`,
`ГОТОВО К ВЫДАЧЕ`.

## Orders, Money And Direct Tasks

Read orders/payment only to understand the card. Write a live order only when
the owner explicitly asks to fill or update that exact ЗН; reread it, preserve
existing rows/payments/dates/comments, write confirmed data and verify again.

For direct VIN/OEM/parts/fluids/repair research, use the relevant focused
playbook. Put only confirmed compact working facts in the card; keep evidence,
confidence and missing checks in the owner report. If the action needs a risky
decision or forbidden write, report the blocker instead.

## Final Report

Report counts for checked, updated, skipped and deferred cards; archive
recommendations; identity/client updates; completed research; and remaining
blockers. State `cards archived=0`, `cards moved=0`, and
`repair_orders_changed=0 and payments_changed=0` unless separately commanded.
Never paste full card contents.

Journal only a genuinely durable workflow conclusion through the guarded
Manager route; never store live board state or customer records.
