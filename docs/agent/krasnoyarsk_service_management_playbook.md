# Krasnoyarsk Service Management Playbook

Purpose: make the AutoStop manager useful beyond one vehicle card:
parts procurement, repair routing, customer flow, staff management, finance
control, CRM hygiene, and knowledge intake for a Krasnoyarsk workshop.

## Director Mode

Autonomous recurring control, deep card-history analysis, prioritization,
internal Telegram follow-up and goal continuation are owned only by
`service_director_manifest.md` and the `run-autostop-director` skill. This file
retains the workshop-domain reference for procurement, repair, staff, customer
flow and finance control.

## Public Traffic Cameras

Public-camera requests are owned by
`.agents/skills/capture-public-camera/SKILL.md`; its canonical allowlist is
`public_camera_registry.json`. This general service playbook adds no second
capture procedure.

## Current CRM Operating Model

Refresh this model with `agent_bootstrap` and `agent_board_digest` when the board changes.

- `Машины в ремзоне`: cars physically in the repair zone; check stale work,
  missing diagnosis, and client/parts blockers first.
- `Запись на ремонт` and `Ресепшен`: appointment and intake flow; check
  date mismatch, no-show risk, and missing VIN/contact details.
- Classify current technician, assistant, and reception columns from live
  `agent_board_digest`. They are load signals, not final payroll records; do not
  hardcode names or labels into durable documentation.
- `Снабжение` and `Заказы запчастей`: procurement blocker queues; every card
  should show the exact part identity, source, price, delivery date, and next
  supplier action.
- `Готовые автомобили`: daily finance and pickup control; check unpaid due
  totals, client pickup status, and closure/archive readiness.

## Procurement

For закупочная цена, local availability, or repair-order materials pricing,
use `parts_search_playbook.md` before filling the ЗН. Keep procurement,
retail, and client sell prices separate.

Use this order:

1. Normalize vehicle identity and part identity from CRM.
2. When the job starts from a CRM card VIN/frame/body number and must be
   written back, use `crm_vin_oem_parts_lookup_playbook.md` for the full
   read/OEM/cross/quote/write/verify pipeline.
3. Confirm OEM or replacement number through the VIN/OEM dossier workflow:
   catalog route, EPC evidence, OEM candidates, supersessions, confidence, and
   missing context.
4. Check contracted supplier/API/price-list routes first, then Drom, ZZap,
   Avito, and other public market sources for local or used/contract options.
5. Rank by exact article, city pickup, delivery time, seller reliability,
   return terms, and total price.
6. Keep the chosen offer in the live CRM/workflow. Store only a reusable search
   rule in manager memory.

Urgency routing:

- Same day: Drom Krasnoyarsk, Avito Krasnoyarsk, local supplier phone checks,
  then ZZap only if pickup/delivery is confirmed.
- 1-3 days: exact OEM in ZZap, Emex, Exist, Autodoc, plus local suppliers.
- Rare/contract parts: Drom, Avito, Автопоставка, donor-vehicle verification,
  seller photo proof, and written return terms.

## Repair Routing

Use this order:

1. Extract complaint, VIN/chassis, mileage, engine, gearbox, and scan data.
2. Route technical facts through `automotive_repair_source_playbook.md`.
3. For safety-critical systems, use OEM or licensed professional information.
4. Create a diagnostic checklist before pricing parts.
5. Keep unsupported facts as questions for the technician.

## Work Labor Pricing

For labor-only estimates, use `work_labor_pricing_playbook.md`; on Gateway v2,
resolve `estimate_repair_work_cost` through raw discovery and schema lookup
before calling it.

Rules:

- Price work separately from parts, fluids, materials, and procurement markup.
- Reconcile exact scope and vehicle context, current public labor-only prices,
  aggregate-only closed-order experience when applicable, and labor-time
  evidence; no one source or formula is a final price by itself.
- Add public norm-hours/labor-time as a second plausibility layer when it can
  be found automatically; do not ask the owner to provide norm-hours.
- Group only comparable operations and comparable vehicle classes.
- Keep `average * 1.50`, rounded to 100 rubles, only as the legacy public
  benchmark. Recommend the reconciled EvidenceBundle result or range, and
  state a conflict rather than forcing an artificial single price.
- Use norm-hours to check complexity and overlapping operations, not as a
  replacement for the public price average.
- If fewer than 3 valid labor-only prices remain, return low confidence and
  missing context instead of a confident price.
- If the card has only a complaint, estimate diagnostics only and return a
  checklist before final repair pricing.
- Do not call `replace_repair_order_works`; ЗН write needs a separate explicit
  owner command.

## Staff Management

Track:

- technician load by active cards
- stalled cards by last event age
- planned vs actual labor hours
- comeback/rework notes
- parts wait time per technician
- unpaid ready cars by responsible owner

Use staff sources only for market context. Real shop compensation and
performance decisions should use internal payroll and actual output.

Do not judge staff only by active card count. Separate productive work from:

- waiting for parts
- waiting for client approval
- waiting for diagnosis data
- waiting for payment
- repeated rework or comeback
- appointment/no-show delays

## Customer Flow

Every active customer-facing card should have:

- next action
- responsible person
- deadline or follow-up point
- visible description if the card needs human reading
- estimate status: requested, prepared, approved, declined, or needs data

## Finance Control

Check daily:

- ready cars with unpaid due amount
- repair orders with materials but no work
- cashbox transactions that need reconciliation
- cards with parts ordered but no client approval
- old inactive cards in repair columns

## Knowledge Intake

New files are source material. Follow `AGENTS.md` and promote only durable
rules into the smallest canonical playbook, catalog, or memory rule.
