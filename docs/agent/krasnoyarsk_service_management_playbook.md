# Krasnoyarsk Service Management Playbook

Purpose: make the AutoStop/Автоспорт manager useful beyond one vehicle card:
parts procurement, repair routing, customer flow, staff management, finance
control, CRM hygiene, and knowledge intake for a Krasnoyarsk workshop.

## Daily Manager Loop

1. Read `today_context`.
2. Read CRM board context with `bootstrap_context` or `get_board_context`.
3. Check overdue cards, ready unpaid cars, parts-order columns, and cars in
   repair zones with no recent event.
4. For each blocker, decide whether the next action is parts, client approval,
   technician assignment, diagnostic data, payment, or archive/ready state.
5. Write `manager_journal` after meaningful changes.

## Current CRM Operating Model

Refresh this model with `get_board_context` when the board changes.

- `Машины в ремзоне`: cars physically in the repair zone; check stale work,
  missing diagnosis, and client/parts blockers first.
- `Запись на ремонт` and `Ресепшен`: appointment and intake flow; check
  date mismatch, no-show risk, and missing VIN/contact details.
- Technician columns such as `В работе, Константин`, `В работе Немец`,
  `В работе Валера`, `Слесарь Сергей`, `Электрик Александр`, `КПП - Дмитрий`,
  and student/assistant columns are load signals, not final payroll records.
- `Снабжение` and `Заказы запчастей`: procurement blocker queues; every card
  should show the exact part identity, source, price, delivery date, and next
  supplier action.
- `Готовые автомобили`: daily finance and pickup control; check unpaid due
  totals, client pickup status, and closure/archive readiness.

## Procurement

For закупочная цена, local availability, or repair-order materials pricing,
use `procurement_pricing_playbook.md` before filling the ЗН. Keep procurement,
retail, and client sell prices separate.

Use this order:

1. Normalize vehicle identity and part identity from CRM.
2. When the job starts from a CRM card VIN/frame/body number and must be
   written back, use `crm_vin_oem_parts_lookup_playbook.md` for the full
   read/OEM/cross/quote/write/verify pipeline.
3. Confirm OEM or replacement number through VIN/OEM routing.
4. Search exact number in Drom, ZZap, Emex/Exist/Autodoc, then local
   Krasnoyarsk suppliers.
5. Rank by exact article, city pickup, delivery time, seller reliability,
   return terms, and total price.
6. Record only the chosen offer and reusable search rule in memory.

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

New files are source material. Follow `knowledge_intake_playbook.md`, then
promote only durable rules into memory or docs.
