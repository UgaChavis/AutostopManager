# Board Cleanup Autopilot Playbook

Purpose: define the standard behavior when the owner says the canonical
command `Приберись`.

The standing board-cleanup command has one spelling: `Приберись`. Do not
document extra natural-language aliases for it.

This is not a separate product feature. It is an operating instruction for the
agent when working through the existing AutoStop CRM MCP tools.

This playbook is the only detailed source of truth for `Приберись`. Route
maps, hot rules, MCP catalogs, and annotations must stay compact and point back
to this contract instead of redefining a parallel behavior.

## Default Authority

`Приберись` means board-management autopilot, with visible card location left
under human control during routine cleanup.

The agent may independently:

- read all active board/card/client context needed for cleanup
- read repair-order and cashbox context only to understand the card; do not
  write repair orders, materials, prices, payments, or cashbox records unless
  the owner gives a separate direct command
- use high-level CRM manager operations (`manager_board_scan`,
  `triage_inbox_cards`, `list_cards_missing_manager_data`,
  `audit_repair_order_consistency`, `audit_client_links`) before focused reads
- update card title, vehicle, description, up to three operational tags, board
  summary, and vehicle profile
- rewrite the public card description into a short, human-readable summary
  with compact paragraphs, sparse visual markers, and supported rich text
  formatting
- leave an empty public description empty unless the owner explicitly asks for
  new text; do not invent a summary just to fill the field
- fill missing vehicle passport fields from the card, repair order, VIN/chassis
  decode, attachments, and source-backed lookup results whenever the data is
  available with adequate confidence, including engine model and gearbox model
  when they can be backed by card/order/VIN/source evidence
- move confirmed structured facts out of the public description after
  successful transfer: phone goes to the client, VIN/plate/mileage and
  агрегаты go to the vehicle passport; keep them in the description only when
  they are still operationally needed or not yet transferred
- analyze client data, compare the card with the client directory, link the
  right client when the match is clear, use phone as the primary match key,
  fill missing client fields, and upsert the client's vehicle when the card
  contains a new confirmed vehicle
- update or fill the repair order only when the owner directly asks to
  `заполнить ЗН`, `заполнить заказ-наряд`, `расписать заказ-наряд`, or gives an
  equivalent explicit repair-order command for the target card
- set or refresh the hidden `board_summary` as the clean 4-5 line board preview
- use `cleanup_card` and `bulk_refresh_board_summaries` in `dry_run` first,
  then `apply` with `actor_name` when the operation is safe
- recommend archive candidates in the report, but do not archive cards unless
  the owner gives a separate explicit owner command for archive
- add short factual questions or conclusions inside the card instead of asking
  the owner in chat
- fill VIN/chassis-derived vehicle fields when source confidence is adequate
- execute direct safe tasks written in the card description, such as finding
  parts, checking OEM numbers, or pricing a maintenance package, and write only
  the concise result back to the card
- source parts and add only the chosen OEM, price, delivery, or verification
  fact into the card, without source lists or long provenance notes
- clean wording, spelling, formatting, and duplicated text while preserving the
  user's meaning

## Manual Single-Card And Multi-Card Mode

`Приберись` may be used for one card, a named group of cards, one operational
bucket such as inbox or ready unpaid cars, or the active board as a whole.
This is always a manual owner-command run.

Interpret scope from the owner's wording:

- `Приберись в карточке <id/name>` means one focused card cleanup.
- `Приберись во входящих`, `Приберись в готовых`, or `Приберись по этим
  карточкам` means a bounded multi-card cleanup for that explicit group.
- `Приберись на доске`, `Приберись во всех активных карточках`, or plain
  `Приберись` means: apply the same one-card cleanup locally to each active
  card that actually needs it, starting from the highest-risk cards.

For multi-card runs:

- start an auditable manager run before changes and close it with counts and
  verification
- read `bootstrap_context` and `manager_board_scan` before selecting cards
- prioritize critical/red cards, inbox cards, ready unpaid cars, stale or
  missing `board_summary`, missing manager-critical data, payment blockers,
  parts blockers, and repair-order consistency issues
- use high-level bulk operations first when they fit the task:
  `bulk_refresh_board_summaries` and `triage_inbox_cards`; timer floors and
  ready-unpaid follow-up writes are separate owner-command routes
- use `cleanup_card(mode=dry_run)` before `apply` for one-card patches
- read `get_card_context` before any card-specific rewrite
- use `expected_updated_at` when available so human edits made during the run
  are not overwritten
- keep each batch small enough to verify; default to the top 10-15 eligible
  cards unless the owner asks to continue
- write only meaningful deltas; skip cards that are already clear
- avoid repeating the same `AI:` note, question, or tag
- leave remaining eligible cards in the final report instead of forcing a large
  noisy pass

For one-card and multi-card runs alike, do not move or archive cards during
`Приберись`. Leave archive recommendations in the report unless the owner gives
a separate explicit archive command for the exact target.

## Context Window Safety

Board-wide cleanup and ready-unpaid scans must be resumable outside the chat
context window.

- start `start_manager_run` before broad board reads, full active-card scans, or
  multi-card write batches
- record `record_manager_run_event` checkpoints after scope selection,
  candidate filtering, every write batch, every skip batch, and final
  verification
- use compact CRM manager tools (`manager_board_scan`,
  `list_ready_unpaid_cards`, `triage_inbox_cards`,
  `list_cards_missing_manager_data`) before raw board dumps
- when a full JSON snapshot is required for machine filtering, save it to a
  private local temp file and put only counts, candidate ids, and the file path
  in chat or the run ledger
- never paste raw board snapshots, phone lists, VIN/license tables, full repair
  orders, or full card dumps into the chat or manager memory
- default to verified batches of 10-15 cards and leave the last processed card,
  skipped ids, blockers, and next batch in the run ledger before continuing
- if a thread stalls after automatic context compaction, resume through
  `list_manager_runs(include_events=true)` and continue from the latest
  checkpoint instead of re-reading the whole board
- if Codex fails immediately after "context automatically compacted" with an
  invalid enum for `context_compaction`, restart Codex Desktop or the active
  `codex app-server` so the running process matches the installed CLI

## Data Preservation Rules

Treat user-entered data as valuable workshop evidence.

Do not delete:

- repair-order works
- repair-order materials
- prices, payments, due totals, prepayments, or cashbox records
- files attached to cards
- client contacts; client records may be enriched from confirmed card evidence,
  but not deleted or merged during `Приберись`
- VIN/chassis/license plate data
- manually written diagnostic findings
- historical notes that explain a decision

Allowed safe edits:

- fix obvious typos
- shorten noisy duplicated text
- restructure text into readable sections
- expand abbreviations when meaning is clear
- add missing factual labels such as `VIN:`, `OEM:`, `Оплата:`, or `Запчасти:`
- append an `AI:` note only when it carries a concise question or conclusion
- normalize and fill client names, phones, links, and client vehicles when the
  matching evidence is clear
- transfer phone, VIN, license plate, mileage, engine, gearbox, and drivetrain
  from noisy public text into the proper client or vehicle-profile fields, then
  remove that duplicate from `description` when the transfer is verified

If a field conflicts with another source, preserve the original and add a short
uncertainty note instead of overwriting blindly.

## Read Order

1. Read `today_context`.
2. Read AutoStop CRM `bootstrap_context`.
3. Run `manager_board_scan`.
4. Run focused manager diagnostics as needed: `triage_inbox_cards`,
   `list_ready_unpaid_cards`, `list_cards_missing_manager_data`,
   `audit_repair_order_consistency`, and `audit_client_links`.
5. Read client candidates with `suggest_clients_for_card`, `search_clients`, or
   `get_client` when card/client data is incomplete or ambiguous.
6. Read recent events with `get_board_events` or the wall preview when needed.
7. Read active cards by focused search, selected batch, or board content.
8. Read repair orders only for cards where money, works, materials, ready
   status, or archive readiness matters.
9. Read card logs only when a card looks stale, contradictory, or manually
   sensitive.

Use focused reads before heavy full-board exports unless a full pass is needed.
Prefer high-level manager operations over long chains of one-card CRUD when the
intent is board diagnosis, timer floor, ready-unpaid follow-up, inbox triage, or
summary refresh.

## Card Action Session

For every card text or vehicle passport write orchestrated by AutoStopManager,
use a standard session:

1. Start `start_manager_run`.
2. Read `agent_brief` and focused CRM context with `get_card_context`.
3. Build a dry-run write contract with MCP `prepare_crm_card_action`
   (`python -m autostop_manager.cli prepare-card-action` only as a local or
   stale-discovery fallback).
4. Write with `update_card` using `expected_updated_at` and `response_mode=compact`.
5. Refresh `board_summary` with `set_card_board_summary` when the public text,
   tags, or passport changed.
6. Reread the card with `get_card_context`.
7. Verify exact `description`, visible text, vehicle_profile field metadata,
   `board_summary_stale=false`, and no unplanned field changes.
8. Record planned patch, write result, diff, verification checks, warnings, and
   final status through the manager run ledger.

Do not treat `prepare_crm_card_action` as a CRM write. It is the preflight
contract that makes the later write faster, auditable, and easier to verify.
If a stale client discovery session does not show it on
`https://crm.autostopcrm.ru/mcp`, run the local CLI preflight and keep its
output with the manager-run evidence.

## Cleanup Passes

Run these passes in order.

### 1. Board Triage

Classify every relevant active card into one current blocker:

- `parts`: needs part number, price, supplier, delivery, or arrival check
- `vin`: VIN/chassis/body number missing or unparsed
- `diagnosis`: complaint exists but diagnostic context is unclear
- `client`: waiting for approval, answer, pickup, appointment, or missing phone
- `payment`: ready/done but due total, payment status, or cashbox status needs
  attention
- `queue`: car is waiting for technician, bay, or appointment date
- `ready`: work appears complete and pickup/closure evidence matters
- `archive_candidate`: card appears finished and can be recommended for human
  archive
- `unclear`: data conflict; leave a short question in the card

### 1a. Vehicle Passport And Client Data

For each target card, clean the structured identity first, then the public text.

Structured fields are the permanent home for identity data. If the existing
description contains phone, VIN/chassis/body number, license plate, mileage,
engine, gearbox, or drivetrain, first try to fill or verify the client and
vehicle-profile fields. After a successful transfer, the public description no
longer needs to repeat that raw identity data unless it is part of the current
repair decision.

Vehicle passport:

- compare card title, vehicle field, description, repair-order text,
  attachments, and existing `vehicle_profile`
- fill stable vehicle fields from source-backed evidence whenever possible:
  make, model, year, VIN/chassis/body number, license plate, mileage, engine
  model, gearbox model, drivetrain, and compact OEM notes
- keep manual fields authoritative when they conflict with weak inferred data
- if engine or gearbox can only be guessed, leave the field unchanged and add a
  short uncertainty note instead of presenting it as confirmed

Client data:

- compare the card with CRM client records before creating or editing a client
- use exact phone as the first matching key; if the phone is present but name is
  missing, search the client directory before writing a new name
- use strong name+vehicle match, VIN/plate history, or previous card links only
  as supporting match evidence when the phone is absent or ambiguous
- link the card to the existing client when the match is clear
- update missing client fields from confirmed card evidence
- when the card shows a confirmed vehicle for a known client, upsert that
  client vehicle with compact vehicle facts
- create a new client only when the card has enough contact data and no clear
  existing client match
- do not delete, merge, or overwrite client records during `Приберись`; leave a
  short duplicate-client warning when matches are plausible but not certain

### 1b. Card Description And Board Preview

The public `description` and hidden `board_summary` are different fields with
different jobs.

`description` is a very concise working summary for a person, not a dump of
everything the agent knows. If the current description is empty, leave it empty
unless the owner explicitly asks to write a public description. If it contains
text, edit it deeply but preserve the meaning: keep task/complaint, key facts,
blocker, money/parts note, confirmed diagnostics, OEM/catalog numbers, prices,
agreements, and useful vehicle data when relevant. Do not write management
blocks such as `Статус:` or `Следующий шаг:` during `Приберись`; the manager
can decide workflow actions from the facts. Do not write long explanations,
source lists, search history, diagnostic theory, broad background, generic
safety/user-protection disclaimers, or agent caveats such as "нужно
перепроверить данные". Preserve valuable old facts, but compress them instead
of expanding them.

When rewriting `description`, make it easy to scan. The default for
`Приберись` is a short formatted summary, not a plain text dump: use rich text
emphasis first and emoji only when it makes scanning faster.

- split meaning into short paragraphs; avoid one dense line of mixed facts
- use **bold** for labels and decisive facts
- use *italic* only for a real uncertainty, caution, or unresolved conflict
- use ++underline++ for the most important amount, OEM/catalog number, approval
  state, or waiting state when emphasis is useful
- use only CRM-supported Markdown syntax: `**bold**`, `*italic*`, and
  `++underline++`; never use raw HTML-style tags for styling
- if a critical blocker/deadline needs emphasis, combine a restrained marker
  such as ⚠️ with **bold** or ++underline++ text
- use restrained emoji markers only when they speed up reading; most cards do
  not need an emoji on every line
- do not decorate every line; formatting should help the mechanic or manager
  understand the card faster
- never add a separate `Статус:` paragraph or `Следующий шаг:` paragraph during
  `Приберись`
- after saving, inspect the visible description/preview; if markup characters
  such as `<...>`, raw tags, or other technical symbols are visible, remove
  them immediately

Preferred public `description` shape:

```markdown
**Авто:** <автомобиль>.

**Задача:** <только важный список или итог>.

**Запчасти/OEM:** **++<номер или выбранный вариант>++**.

**Деньги:** **++<сумма или согласование>++**.

*Важно:* <короткий риск, blocker, проверка или условие, если есть>.
```

Use only the blocks that matter for the card. For a tiny card, two paragraphs
are enough. Avoid decorative formatting, long source/provenance blocks, and
verbose AI explanations. Preserve technical data, part numbers, prices,
payments, diagnostics, files, and history exactly when they are still relevant;
move contacts and raw identifiers to structured fields when possible. If old
text is noisy, reduce it to the important facts only.

Bad public `description` patterns:

```markdown
Статус: ...
Следующий шаг: ...
По данным источников нужно перепроверить...
В целях безопасности пользователя...
AI: длинное объяснение поиска и всех источников...
```

Keep `board_summary` plain, compact, and free of decorative formatting.

`board_summary` is the compact operator preview shown on the board. Update it
with `set_card_board_summary` after card text/profile/tag changes. Keep it to
four or five short lines and do not include phone numbers, VIN, full client
identity, raw scan dumps, or long issue lists.

Recommended `board_summary` shape:

```text
<vehicle or job>.
Факт: <main issue, work, or result>.
Деньги/запчасти: <only if relevant>.
Важно: <one blocker, caveat, or missing fact if needed>.
```

If the card is incomplete:

```text
Данные по обращению неполные.
Не хватает: VIN/госномер/список работ.
В описании сохранить только известные факты.
```

Preserve useful old facts instead of deleting them. Do not put long AI
reasoning or source lists into either field. Rich formatting belongs mainly in
the public `description`; `board_summary` should stay plain so the board
preview remains stable.

### 1c. Title, Vehicle, And Tags

During cleanup, analyze `title`, `vehicle`, and `description` together.

Title and vehicle:

- keep `vehicle` as a compact make/model display, not a repair task
- keep `title` as the short essence of the issue, work, or result
- update either field only when the current card text clearly supports a better
  value
- do not rewrite title or vehicle just for style if they are already clear

Tags:

- use tags sparingly; they are action cues, not decoration
- keep no more than three tags on a card
- add or remove a tag only when it changes what the operator should notice or
  do
- use informational/green-style tags for facts such as arrived parts
- use waiting/yellow-style tags for client, diagnosis, parts, or approval
  blockers
- use urgent/red-style tags only for critical blockers, payment problems, or
  contradictions
- do not delete existing useful tags during cosmetic text cleanup

### 1d. Repair Order And Payment Boundary

`Приберись` by itself does not authorize repair-order writes. During ordinary
cleanup, read repair orders when they explain money, work, materials, payment,
or completion state, then summarize the relevant facts in the card description
and board summary.

`Приберись` also does not authorize payment or cashbox writes. During cleanup,
payments and cashbox records are read-only evidence. Never create, delete,
move, or edit payments, cash transactions, cashboxes, material prices, work
prices, or repair-order totals unless the owner gives a separate direct command
for that exact target.

Only fill or rewrite a live repair order when the owner explicitly asks for
that target, for example `заполни заказ-наряд`, `распиши ЗН`, `добавь работы в
заказ-наряд`, or `обнови материалы в ЗН`. In that case:

- reread the card and repair order first
- preserve existing works, materials, prices, payments, comments, and dates
- write only confirmed work/material/payment rows from the owner's request or
  trusted source text
- verify the repair order after saving and report what changed

### 1e. Direct Tasks Found In The Card

If the existing card description contains a direct safe work command, perform
it during `Приберись` instead of only reformatting the text.

Examples:

- `найти запчасть`, `найти OEM`, `подобрать аналог`, `проверить наличие`
- `проценить ТО`, `посчитать ориентир`, `проверить регламент`
- `уточнить по VIN`, `расшифровать VIN`, `найти модель двигателя/коробки`

Rules:

- execute the task only when it can be done without repair-order, payment, or
  cashbox writes
- use source-backed lookup routes for VIN/OEM, parts, fluids, and technical
  data
- write back only a compact result: selected OEM/number, price/range, supplier
  or delivery cue, and confidence caveat when needed
- if the task needs forbidden write access or a risky business decision, leave
  a short blocker instead of acting

### 2. Vehicle Identity

If a card contains a VIN, chassis number, or body code:

- classify identifier type first
- use vehicle identity and VIN/OEM playbooks
- fill stable vehicle profile fields only, including engine and gearbox when
  evidence is adequate
- do not present inferred trim/options as confirmed
- add short uncertainty when confidence is incomplete

### 3. Parts And Procurement

If a card asks for a detail/part:

- normalize part name, side, axle, position, condition, and OEM if present
- when VIN/chassis/body number plus writeback is required, use
  `docs/agent/crm_vin_oem_parts_lookup_playbook.md`
- build a VIN/OEM dossier when VIN/chassis is available; if EPC evidence is
  missing, record missing context instead of guessing an OEM number
- search exact OEM first
- use Drom, ZZap, Avito, Emex, Exist, Autodoc, and local Krasnoyarsk suppliers
  according to urgency
- add only concise useful output to the card: OEM, price range, delivery or
  pickup, and verification caveat when needed

Preferred note format:

```text
AI: OEM 90311-89014. Наличие: Drom/ZZap, Красноярск.
```

### 4. Repair And Technical Data

If a card contains a repair complaint:

- extract complaint, VIN/chassis, mileage, engine, transmission, and scan data
- route technical facts through repair-source playbooks
- never invent torque, fluid, pinout, labor, programming, SRS, ADAS, HV, or
  immobilizer data
- write only the confirmed diagnostic fact, missing data, or concise question

Preferred note format:

```text
AI: скан/ошибки не указаны; деталь до диагностики не подтверждена.
```

### 5. Customer Flow

Every active customer-facing card should show:

- who/what is waited on
- follow-up point when useful
- approval/payment/pickup state when visible
- the factual blocker when it is already clear

Preferred note format:

```text
AI: ждем согласование клиента по смете.
```

### 6. Finance And Ready Cars

For ready/done cards:

- read repair-order status and due totals
- check payment status before archive
- if unpaid or unclear, keep visible and mark payment blocker
- if paid/settled and no blockers remain, leave a short archive recommendation
  instead of archiving automatically

Preferred note format:

```text
AI: авто готово, вижу остаток к оплате.
```

### 7. Stale Cards

If an active card has not changed for a long time:

- read card context/log before writing
- identify likely blocker
- set or adjust a tag if useful; change indicators or deadlines only when the
  owner explicitly asks for timer/signal work for the exact target
- add one short `AI:` question if human input is needed

Preferred note format:

```text
AI: давно без движения, по детали нет подтверждения.
```

## Write Rules

Before every CRM write:

- know exact `card_id` or target id
- preserve existing user-entered content
- make the smallest useful update
- keep card text short
- if the public description is empty, leave it empty unless the owner explicitly
  asked for new text
- transfer phone, VIN, license plate, mileage, engine, gearbox, and drivetrain
  into structured client/vehicle fields when the evidence is clear; after
  verifying the transfer, do not keep that raw data in the public summary just
  because it used to be there
- treat repair-order, payment, cashbox, materials, works, and prices as
  read-only unless the owner explicitly asked to change that exact target
- client records and client vehicles may be enriched from clear evidence, but
  do not delete or merge clients during `Приберись`
- when touching `description`, keep it short, recoverable, and readable:
  important facts only, split into useful paragraphs, not a full report, not a
  source log, and not a status/next-step instruction for the manager
- when rewriting `description`, actively apply supported rich text formatting
  and restrained emoji markers; use **bold**, *italic*, and ++underline++ for
  real emphasis, not decoration
- never write raw HTML or pseudo-formatting into a CRM description; visible
  markup is worse than no formatting
- do not add sources, long explanations, or extra background into the card
- after touching `description`, `title`, `tags`, `vehicle_profile`, or client
  link/vehicle facts that affect the board preview, call `set_card_board_summary`
  and verify `board_summary_stale=false`
- avoid multiple noisy notes when one structured update is enough
- do not rewrite a card just for style if it already reads clearly
- do not move or archive cards during `Приберись`; leave the current column
  and archive state as-is unless the owner gives a separate explicit owner
  command for that target

Use the card itself for operational questions. Ask the owner in chat only when:

- a destructive action is needed
- payment/cashbox data conflict and cannot be resolved from CRM
- a client-sensitive decision needs owner judgment
- source-backed technical data is unavailable for a safety-critical action

## Tag And Indicator Heuristics

Tags should be operational and short:

- `НУЖЕН VIN`
- `ЖДЕТ ЗАПЧАСТИ`
- `ЗАПЧАСТИ ПРИШЛИ`
- `СОГЛАСОВАТЬ`
- `ЖДЕТ ОПЛАТЫ`
- `НУЖНА ДИАГНОСТИКА`
- `ЖДЕМ КЛИЕНТА`
- `ГОТОВО К ВЫДАЧЕ`

Tags are part of ordinary `Приберись`. Indicators and deadlines are not the
main cleanup surface; change them only when the current card state clearly
requires it or the owner explicitly asked for timer/signal work.
Use no more than three tags and avoid tag changes when a concise description or
board summary already carries the information well.

Indicator:

- red: payment blocker, urgent stale card, missing critical data, serious
  contradiction
- yellow: waiting for parts/client/diagnosis or a known non-urgent blocker
- green: ready/clear/no blocker

## Column Movement Boundary

Do not move or archive cards during board-cleanup autopilot, even when the next
operational state looks clear. Use non-moving, non-archive updates instead:

- tags
- readable public description
- hidden `board_summary`
- short `AI:` note
- vehicle-profile enrichment
- client link and client vehicle enrichment
- archive recommendation in the final report

Move or archive a card only when the owner gives a separate explicit owner
command with the target card and requested destination or archive action.

## Final Report

Report to the owner briefly:

- cards checked
- cards updated
- cards skipped or left for a next batch when the manual scope is large
- cards archived=0 unless explicitly commanded
- archive recommendations
- cards left in their current columns by rule
- VIN/OEM/parts work done
- vehicle passport and client data updates
- repair_orders_changed=0 and payments_changed=0 unless explicitly commanded
- blockers that still need human decision
- risks or data gaps

Do not paste full card contents into the report.

## Memory

After cleanup, write `manager_journal` with:

- cleanup date/time
- major actions
- important unresolved blockers
- durable workflow lessons, if any

Do not store live board state, full card text, client records, or cashbox data
in AutostopManager memory.
