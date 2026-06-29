# Board Cleanup Autopilot Playbook

Purpose: define the standard behavior when the owner says the canonical command
`Приберись`.

The command has one spelling: `Приберись`. Do not document extra
natural-language aliases for it.

This playbook is the only detailed source of truth for `Приберись`. Route maps,
hot rules, MCP catalogs, and annotations must stay compact and point back here
instead of redefining parallel behavior.

## Authority

`Приберись` is manual owner-command cleanup for one card, an explicit group, or
the active board. It is not card movement, archive, timer-floor work, payment
work, cashbox work, or repair-order rewriting.

The agent may:

- read active board/card/client context needed for cleanup
- read repair-order and cashbox context as evidence only
- use manager diagnostics such as `manager_board_scan`, `triage_inbox_cards`,
  `list_cards_missing_manager_data`, `audit_repair_order_consistency`, and
  `audit_client_links`
- update confirmed card title, compact vehicle, existing public description,
  up to three operational tags, hidden board summary, vehicle profile, clear
  client link, and confirmed client vehicle facts
- execute direct safe tasks already written in the card, such as finding parts,
  checking OEM numbers, decoding VIN/frame, or pricing a maintenance package
- recommend archive candidates in the final report

The agent must not move, archive, delete, change deadlines/indicators, edit
repair-order works/materials/prices/totals, payments, or cashboxes unless the
owner gives a separate explicit command for that exact target.

## Scope

- `Приберись в карточке <id/name>`: one focused card.
- `Приберись во входящих`, `Приберись в готовых`, or `Приберись по этим
  карточкам`: bounded cleanup for that explicit group.
- Plain `Приберись`: apply one-card cleanup to active cards that actually need
  it, starting from highest-risk candidates.

For multi-card runs:

- start a manager run before changes and close it with counts and verification
- read `bootstrap_context` and `manager_board_scan`
- prioritize critical/red cards, inbox, ready unpaid, stale/missing
  `board_summary`, missing manager data, payment blockers, parts blockers, and
  repair-order consistency issues
- use high-level bulk tools first when they fit; timer floor and ready-unpaid
  followups are separate routes
- use `cleanup_card(mode=dry_run)` before `apply`
- keep batches small enough to verify; default 10-15 eligible cards unless the
  owner asks to continue
- skip cards that are already clear

## Context Safety

Board-wide work must survive context compaction:

- use `start_manager_run`, `record_manager_run_event`, and `finish_manager_run`
- checkpoint scope, candidates, each write batch, each skip batch, and final
  verification
- prefer compact CRM manager tools before raw exports
- keep raw board dumps, phone lists, VIN/license tables, and full repair orders
  out of chat and durable memory
- resume from `list_manager_runs(include_events=true)` after a stalled or
  compacted thread

## Data Preservation

Preserve user-entered CRM evidence:

- repair-order works/materials/prices/totals
- payments, due totals, prepayments, cashbox records
- attachments and historical notes
- client contacts
- VIN/chassis/license data
- manual diagnostics

Safe edits are small and factual: fix typos, remove duplicated noise, structure
text into readable paragraphs, add missing labels, enrich clear client/vehicle
fields, and append one short `AI:` note only when it adds a factual question or
conclusion.

If sources conflict, preserve the original and add a short uncertainty note
instead of overwriting blindly.

## Read Order

1. `today_context`
2. CRM `bootstrap_context`
3. `manager_board_scan`
4. Focused diagnostics: `triage_inbox_cards`, `list_ready_unpaid_cards`,
   `list_cards_missing_manager_data`, `audit_repair_order_consistency`,
   `audit_client_links`
5. Client candidates: `suggest_clients_for_card`, `search_clients`, `get_client`
6. Focused card/context reads
7. Repair orders only where works, money, completion, or blockers matter
8. Card logs only for stale, contradictory, or sensitive cards

## Card Action Session

For card text or vehicle passport writes orchestrated by AutostopManager:

1. Start or reuse a manager run.
2. Read `agent_brief` and focused `get_card_context`.
3. Build a dry-run contract with `prepare_crm_card_action`.
4. Write with `update_card` using `expected_updated_at` when available.
5. Update `board_summary` with `set_card_board_summary` when text/profile/tags
   changed.
6. Reread and verify description, visible text, vehicle_profile metadata,
   `board_summary_stale=false`, and no unplanned field changes.
7. Record planned patch, write result, diff, warnings, and verification.

## Structured Identity First

Clean structured fields before public text.

- phone goes to the client
- VIN/plate/mileage and агрегаты go to the vehicle passport
- keep manual vehicle/profile fields authoritative
- fill make/model/year/VIN/chassis/plate/mileage/engine/gearbox/drivetrain only
  from card/order/attachment/VIN/source-backed evidence
- link an existing client by exact phone first; use name+vehicle, VIN/plate
  history, or previous links only as supporting evidence
- create a new client only when there is enough contact data and no clear match
- never delete, merge, or force ambiguous client records during `Приберись`

After verified transfer, the public description normally should not repeat raw
phone, VIN, plate, mileage, engine, gearbox, or drivetrain unless still
operationally needed.

## Description And Board Summary

Public `description` and hidden `board_summary` are different fields.

If `description` is empty, leave it empty unless the owner explicitly asks for
new public text. If it contains text, compress it into a short working summary:
task/complaint, key facts, blocker, money/parts note, confirmed diagnostics,
OEM/catalog numbers, prices, agreements, and useful vehicle facts only.

Do not add separate `Статус:` or `Следующий шаг:` blocks. Do not write source
lists, search history, diagnostic theory, generic safety disclaimers,
`нужно перепроверить данные`, or verbose AI explanations.

Readable formatting rules:

- split dense text into short paragraphs
- use **bold** for labels and decisive facts
- use *italic* only for real uncertainty or caution
- use ++underline++ for one key amount, OEM/catalog number, approval, or waiting
  state when emphasis helps
- use restrained emoji markers only when they speed scanning
- use only CRM-supported Markdown: `**bold**`, `*italic*`, `++underline++`
- never use raw HTML-style tags or pseudo-formatting
- after saving, inspect visible preview and remove any visible technical markup

Preferred public `description` shape:

```markdown
**Авто:** <автомобиль>.

**Задача:** <важный факт или итог>.

**Запчасти/OEM:** **++<номер или выбранный вариант>++**.

**Деньги:** **++<сумма или согласование>++**.

*Важно:* <короткий риск или blocker, если есть>.
```

Use only blocks that matter. Two short paragraphs are enough for a tiny card.

Bad public `description` patterns:

```markdown
Статус: ...
Следующий шаг: ...
По данным источников нужно перепроверить...
В целях безопасности пользователя...
AI: длинное объяснение поиска и всех источников...
```

`board_summary` is the compact board preview. Keep it plain, stable, and no
longer than four or five short lines. Do not include phone, VIN, full client
identity, raw scan dumps, rich formatting, source lists, or long issue lists.

Recommended `board_summary` shape:

```text
<vehicle or job>.
Факт: <main issue, work, or result>.
Деньги/запчасти: <only if relevant>.
Важно: <one blocker, caveat, or missing fact if needed>.
```

## Title, Vehicle, Tags

- keep `vehicle` as a compact make/model display
- keep `title` as the short essence of issue, work, or result
- update either only when card evidence clearly supports a better value
- use tags sparingly; no more than three tags
- tags are action cues, not decoration
- do not delete useful existing tags during cosmetic cleanup

Typical tags: `НУЖЕН VIN`, `ЖДЕТ ЗАПЧАСТИ`, `ЗАПЧАСТИ ПРИШЛИ`,
`СОГЛАСОВАТЬ`, `ЖДЕТ ОПЛАТЫ`, `НУЖНА ДИАГНОСТИКА`, `ЖДЕМ КЛИЕНТА`,
`ГОТОВО К ВЫДАЧЕ`.

Indicators and deadlines are not the main cleanup surface. Change them only
when the owner explicitly asks for timer/signal work or the exact target state
clearly requires it.

## Repair Order, Payment, Cashbox

`Приберись` does not authorize repair-order, payment, or cashbox writes. Read
those records only to understand the card.

Only fill or rewrite a live repair order when the owner explicitly asks for that
target, for example `заполни заказ-наряд`, `распиши ЗН`, `добавь работы в
заказ-наряд`, or `обнови материалы в ЗН`. Then reread first, preserve existing
rows/payments/dates/comments, write only confirmed rows, and verify after
saving.

## Direct Safe Tasks

If the current card already asks for a safe task, perform it during cleanup:

- find part, OEM, analog, availability
- price maintenance or a small package
- decode VIN/frame or find engine/gearbox model

Use the relevant VIN/OEM, parts, fluids, and repair-source routes. Write back
only a compact result: selected OEM/article, price/range, supplier/delivery
cue, and confidence caveat when needed.

If the task needs forbidden write access or a risky business decision, leave a
short blocker instead of acting.

## Technical And Customer Flow Checks

- For VIN/chassis/body code: classify identifier first, then use vehicle
  identity and VIN/OEM playbooks.
- For repair complaints: extract complaint, vehicle context, mileage, scan data;
  never invent torque, fluid, pinout, labor, programming, SRS, ADAS, HV, or
  immobilizer facts.
- For customer-flow cards: show who/what is waited on, approval/payment/pickup
  state, and the factual blocker.
- For ready/done cards: read order status and due totals; if paid/settled and
  no blockers remain, leave archive recommendation instead of archiving.
- For stale cards: read context/log, identify likely blocker, and add one short
  `AI:` question if needed.

## Write Rules

Before every CRM write:

- know exact `card_id` or target id
- preserve existing user-entered content
- make the smallest useful update
- keep card text short
- use `expected_updated_at` when available
- refresh `board_summary` after text/profile/tag changes that affect preview
- avoid repeated noisy notes
- do not rewrite a card just for style if it already reads clearly
- do not move or archive cards during `Приберись`

Ask the owner in chat only when a destructive action, money conflict,
client-sensitive decision, or safety-critical missing source needs judgment.

## Final Report

Report briefly:

- cards checked, updated, skipped, and left for later
- cards archived=0 and cards moved=0 unless separately commanded
- archive recommendations
- vehicle passport/client updates
- VIN/OEM/parts work done
- repair_orders_changed=0 and payments_changed=0 unless separately commanded
- remaining blockers, risks, and data gaps

Do not paste full card contents into the report.

## Memory

After cleanup, write a compact `manager_journal` entry with date/time, major
actions, unresolved blockers, and durable workflow lessons if any. Do not store
live board state, full card text, client records, or cashbox data in manager
memory.
