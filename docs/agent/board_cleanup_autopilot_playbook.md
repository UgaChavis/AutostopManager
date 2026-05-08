# Board Cleanup Autopilot Playbook

Purpose: define the standard behavior when the owner says `Приберись`,
`прибейсь`, `переберись`, `прибери доску`, `обслужи доску`,
`актуализируй доску`, or asks for a routine board cleanup.

This is not a separate product feature. It is an operating instruction for the
agent when working through the existing AutoStop CRM MCP tools.

## Default Authority

`Приберись` means board-management autopilot, with visible card location left
under human control during routine cleanup.

The agent may independently:

- read all active board/card/repair-order/cashbox context needed for cleanup
- update card title, vehicle, description, tags, deadline, indicator, and
  vehicle profile
- rewrite the public card description into a clean detailed working note
  using supported rich text formatting and restrained emoji markers
- update the hidden `board_summary` through `set_card_board_summary` so the
  board tile shows a four-or-five-line operator preview
- leave archive recommendations in the card/report when a card appears
  completed; do not archive automatically during routine cleanup
- add short questions or recommendations inside the card instead of asking the
  owner in chat
- fill VIN/chassis-derived vehicle fields when source confidence is adequate
- source parts and add short OEM/price/source conclusions into the card
- clean wording, spelling, formatting, and duplicated text while preserving the
  user's meaning

## Automation Mode

When `Приберись` runs on a schedule, treat it as an incremental control pass,
not a reason to rewrite the whole board every hour.

Hourly automation should:

- read current memory and live CRM state first
- prioritize red/overdue/stale cards, ready cars, payment blockers, parts
  blockers, and cards with missing critical identity data
- write only meaningful deltas
- avoid repeating the same `AI:` note if the card already contains the same
  question or conclusion
- avoid resetting deadlines every hour unless the operational state changed
- avoid style-only rewrites on cards that are already clear
- stop without writes if CRM/MCP status is unhealthy or target card identity is
  uncertain

Hourly automation may still update descriptions, board summaries, tags,
indicators, deadlines, and safe vehicle fields when the usual safety rules below
are satisfied. It must not move cards between columns or archive cards unless
the owner explicitly asks for one exact target/action. The final report should
be compact: checked, changed, moved=0, archived=0 unless explicitly requested,
archive recommendations, blockers, and any risks.

## Data Preservation Rules

Treat user-entered data as valuable workshop evidence.

Do not delete:

- repair-order works
- repair-order materials
- prices, payments, due totals, prepayments, or cashbox records
- files attached to cards
- client contacts
- VIN/chassis/license plate data
- manually written diagnostic findings
- historical notes that explain a decision

Allowed safe edits:

- fix obvious typos
- shorten noisy duplicated text
- restructure text into readable sections
- format the public description with clear headings, **bold** key labels,
  *italic* clarifications, underlined emphasis when the CRM editor supports it,
  bullet/check lists, and restrained emoji markers such as `🔧`, `✅`, `⚠️`,
  and `💰`
- expand abbreviations when meaning is clear
- add missing labels such as `VIN:`, `OEM:`, `Следующий шаг:`
- append an `AI:` note with a concise question or conclusion

If a field conflicts with another source, preserve the original and add a short
uncertainty note instead of overwriting blindly.

## Read Order

1. Read `today_context`.
2. Read AutoStop CRM `bootstrap_context`.
3. Read `get_board_context`.
4. Read recent events with `get_board_events` or the wall preview when needed.
5. Read active cards by focused search or board content.
6. Read repair orders only for cards where money, works, materials, ready
   status, or archive readiness matters.
7. Read card logs only when a card looks stale, contradictory, or manually
   sensitive.

Use focused reads before heavy full-board exports unless a full pass is needed.

## Cleanup Passes

Run these passes in order.

### 1. Board Triage

Classify every relevant active card into one current blocker:

- `parts`: needs part number, price, supplier, delivery, or arrival check
- `vin`: VIN/chassis/body number missing or unparsed
- `diagnosis`: complaint exists but diagnostic next step is unclear
- `client`: waiting for approval, answer, pickup, appointment, or missing phone
- `payment`: ready/done but due total, payment status, or cashbox status needs
  attention
- `queue`: car is waiting for technician, bay, or appointment date
- `ready`: work appears complete and pickup/closure is next
- `archive_candidate`: card appears finished and can be recommended for human
  archive
- `unclear`: data conflict; leave a short question in the card

### 1a. Card Description And Board Preview

The public `description` and hidden `board_summary` are different fields with
different jobs.

`description` is the detailed recoverable card text. It may include vehicle
identity, customer context, VIN, work list, diagnostics, money, parts, and
history. Keep it readable and preserve useful old text under `Подробности:` if
needed.

When rewriting `description`, use the CRM text editor deliberately: headings,
bold labels, light italic comments, underline only for important warnings or
money/client approvals, short lists, and a few useful emoji markers. Preserve
technical data, part numbers, prices, payments, contacts, diagnostics, files,
and history exactly; do not let formatting turn into data loss. Keep
`board_summary` plain, compact, and free of decorative formatting.

`board_summary` is the compact operator preview shown on the board. Update it
with `set_card_board_summary` after card text/profile/tag changes. Keep it to
four or five short lines and do not include phone numbers, VIN, full client
identity, raw scan dumps, or long issue lists.

Recommended `board_summary` shape:

```text
Что сейчас: <main issue or job>.
Стадия: <diagnosis / agreement / waiting / repair / pickup>.
Следующее действие: <one concrete operator step>.
Важно: <one deadline/payment/parts/blocker if needed>.
```

If the card is incomplete:

```text
Что сейчас: не хватает данных по обращению.
Стадия: входящие / уточнение.
Следующее действие: запросить недостающие данные.
Важно: указать VIN/госномер/список работ в описании, но не в превью.
```

Preserve useful old description text instead of deleting it. Do not put long AI
reasoning into either field.

### 2. Vehicle Identity

If a card contains a VIN, chassis number, or body code:

- classify identifier type first
- use vehicle identity and VIN/OEM playbooks
- if `engine_model`, `gearbox_model`, or `drivetrain` is empty, try to enrich
  those fields from source-backed VIN/chassis/frame data
- use local knowledge and `lookup_original_parts` first, then internet search
  only when the current source needs confirmation
- fill stable vehicle profile fields only when confidence is adequate
- preserve manual fields; never overwrite operator-entered aggregate data
- do not present inferred trim/options as confirmed
- when several variants are possible, leave the field empty and add short
  uncertainty to `oem_notes` / `tentative_fields`

### 3. Parts And Procurement

If a card asks for a detail/part:

- normalize part name, side, axle, position, condition, and OEM if present
- find OEM number when VIN/chassis is available
- search exact OEM first
- use Drom, ZZap, Avito, Emex, Exist, Autodoc, and local Krasnoyarsk suppliers
  according to urgency
- add only concise useful output to the card: OEM, price range, source, delivery
  or pickup, and next action

Preferred note format:

```text
AI: OEM 90311-89014. Проверить наличие: Drom/ZZap, Красноярск.
```

### 4. Repair And Technical Data

If a card contains a repair complaint:

- extract complaint, VIN/chassis, mileage, engine, transmission, and scan data
- route technical facts through repair-source playbooks
- never invent torque, fluid, pinout, labor, programming, SRS, ADAS, HV, or
  immobilizer data
- write the next diagnostic action in short form

Preferred note format:

```text
AI: нужен скан/ошибки перед заказом детали.
```

### 5. Customer Flow

Every active customer-facing card should show:

- next action
- who/what is waited on
- deadline or follow-up point when useful
- approval/payment/pickup state when visible

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
- set indicator/deadline/tag if useful
- add one short `AI:` question if human input is needed

Preferred note format:

```text
AI: давно без движения. Следующий шаг: подтвердить статус детали.
```

## Write Rules

Before every CRM write:

- know exact `card_id` or target id
- preserve existing user-entered content
- make the smallest useful update
- keep card text short
- when touching `description`, keep it detailed and recoverable rather than
  treating its first lines as the board preview
- when rewriting `description`, apply supported rich text formatting and
  restrained emoji markers without changing technical facts, prices, payments,
  contacts, diagnostics, or historical notes
- after touching `description`, `title`, `tags`, or `vehicle_profile`, call
  `set_card_board_summary` and verify `board_summary_stale=false`
- avoid multiple noisy notes when one structured update is enough
- do not rewrite a card just for style if it already reads clearly
- do not move cards between columns during `Приберись` / `прибейсь` /
  `переберись`; leave the current column as-is unless the owner gives a
  separate explicit move command
- do not archive cards during routine cleanup; leave an archive recommendation
  unless the owner gives a separate explicit archive command

Use the card itself for operational questions. Ask the owner in chat only when:

- a destructive action beyond archive is needed
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

Indicator:

- red: payment blocker, urgent stale card, missing critical data, serious
  contradiction
- yellow: waiting for parts/client/diagnosis but next action is known
- green: ready/clear/no blocker

## Column Movement Boundary

Do not move cards between columns during board-cleanup autopilot, even when the
next operational state looks clear. Use non-moving updates instead:

- tags
- indicators
- deadlines
- detailed description
- hidden board summary
- short `AI:` note
- vehicle-profile enrichment
- archive recommendation when a card looks completed

Move a card only when the owner gives a separate explicit command with the
target card and target column.

## Final Report

Report to the owner briefly:

- cards checked
- cards updated
- cards archived only if explicitly requested
- archive recommendations
- cards left in their current columns by rule
- VIN/OEM/parts work done
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
