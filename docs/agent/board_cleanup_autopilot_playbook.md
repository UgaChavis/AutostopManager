# Board Cleanup Autopilot Playbook

Purpose: define the standard behavior when the owner says the canonical
command `Приберись`.

Older spellings produced by voice input or earlier docs are deprecated. Do not
list them as command aliases; the standing board-cleanup command is one word:
`Приберись`.

This is not a separate product feature. It is an operating instruction for the
agent when working through the existing AutoStop CRM MCP tools.

## Default Authority

`Приберись` means board-management autopilot, with visible card location left
under human control during routine cleanup.

The agent may independently:

- read all active board/card/repair-order/cashbox context needed for cleanup
- use high-level CRM manager operations (`manager_board_scan`,
  `triage_inbox_cards`, `list_cards_missing_manager_data`,
  `audit_repair_order_consistency`, `audit_client_links`) before focused reads
- update card title, vehicle, description, tags, deadline, indicator, and
  vehicle profile
- rewrite the public card description into a short, human-readable working note
  with paragraphs, useful visual markers, and supported rich text formatting
- fill missing vehicle passport fields from the card, repair order, VIN/chassis
  decode, attachments, and source-backed lookup results whenever the data is
  available with adequate confidence
- update or fill the repair order only when the owner directly asks to
  `заполнить ЗН`, `заполнить заказ-наряд`, `расписать заказ-наряд`, or gives an
  equivalent explicit repair-order command for the target card
- set or refresh the hidden `board_summary` as the clean 4-5 line board preview
- use `cleanup_card`, `bulk_refresh_board_summaries`,
  `bulk_set_deadline_if_below`, and `apply_ready_unpaid_followups` in
  `dry_run` first, then `apply` with `actor_name` when the operation is safe
- recommend archive candidates in the report, but do not archive cards unless
  the owner gives a separate explicit owner command for archive
- add short factual questions or conclusions inside the card instead of asking
  the owner in chat
- fill VIN/chassis-derived vehicle fields when source confidence is adequate
- source parts and add only the chosen OEM, price, delivery, or verification
  fact into the card, without source lists or long provenance notes
- clean wording, spelling, formatting, and duplicated text while preserving the
  user's meaning

## Automation Mode

When `Приберись` runs on a schedule, treat it as an incremental control pass,
not a reason to rewrite the whole board every hour.

Hourly automation should:

- read current memory and live CRM state first
- run `manager_board_scan` before lower-level card loops
- prioritize red/overdue/stale cards, ready cars, payment blockers, parts
  blockers, and cards with missing critical identity data
- write only meaningful deltas
- avoid repeating the same `AI:` note if the card already contains the same
  question or conclusion
- avoid resetting deadlines every hour unless the operational state changed
- avoid style-only rewrites on cards that are already clear
- stop without writes if CRM/MCP status is unhealthy or target card identity is
  uncertain

Hourly automation may still update descriptions, `board_summary`, tags,
indicators, deadlines, and safe vehicle fields when the usual safety rules
below are satisfied. It must not move or archive cards unless the owner
explicitly asks for one exact target/action. The final report should be compact:
checked, changed, moved=0, archived=0 unless explicitly commanded, archive
recommendations, blockers, and any risks.

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
- expand abbreviations when meaning is clear
- add missing factual labels such as `VIN:`, `OEM:`, `Оплата:`, or `Запчасти:`
- append an `AI:` note only when it carries a concise question or conclusion

If a field conflicts with another source, preserve the original and add a short
uncertainty note instead of overwriting blindly.

## Read Order

1. Read `today_context`.
2. Read AutoStop CRM `bootstrap_context`.
3. Run `manager_board_scan`.
4. Run focused manager diagnostics as needed: `triage_inbox_cards`,
   `list_ready_unpaid_cards`, `list_cards_missing_manager_data`,
   `audit_repair_order_consistency`, and `audit_client_links`.
5. Read recent events with `get_board_events` or the wall preview when needed.
6. Read active cards by focused search or board content.
7. Read repair orders only for cards where money, works, materials, ready
   status, or archive readiness matters.
8. Read card logs only when a card looks stale, contradictory, or manually
   sensitive.

Use focused reads before heavy full-board exports unless a full pass is needed.
Prefer high-level manager operations over long chains of one-card CRUD when the
intent is board diagnosis, timer floor, ready-unpaid follow-up, inbox triage, or
summary refresh.

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

### 1a. Card Description And Board Preview

The public `description` and hidden `board_summary` are different fields with
different jobs.

`description` is a concise working note for a person, not a dump of everything
the agent knows. Keep only the card substance: task/complaint, key facts,
blocker, money/parts note, confirmed diagnostics, and useful vehicle data when
relevant. Do not write management blocks such as `Статус:` or `Следующий шаг:`
during `Приберись`; the manager can decide workflow actions from the facts. Do
not write long explanations, source lists, search history, diagnostic theory,
or broad background. Preserve valuable old facts, but compress them into
readable paragraphs instead of expanding them.

When rewriting `description`, make it easy to scan. The default for
`Приберись` is a formatted public note, not a plain text dump: use emoji
section markers and at least one rich-text accent in any substantial rewritten
description unless the CRM renderer is broken.

- split meaning into short paragraphs; avoid one dense line of mixed facts
- use **bold** for labels and decisive facts
- use *italic* for uncertainty, caution, or "needs verification"
- use ++underline++ for the most important amount, OEM/catalog number, approval
  state, or waiting state when emphasis is useful
- use only CRM-supported Markdown syntax: `**bold**`, `*italic*`, and
  `++underline++`; never use raw HTML-style tags for styling
- if a critical blocker/deadline needs emphasis, combine a restrained marker
  such as ⚠️ with **bold** or ++underline++ text
- use restrained emoji markers only when they speed up reading, for example
  🔧 work, 🧪 diagnostics, 📦 parts, 💰 money, ⚠️ blocker
- do not decorate every line; formatting should help the mechanic or manager
  understand the card faster
- never add a separate `Статус:` paragraph or `Следующий шаг:` paragraph during
  `Приберись`
- after saving, inspect the visible description/preview; if markup characters
  such as `<...>`, raw tags, or other technical symbols are visible, remove
  them immediately

Preferred public `description` shape:

```markdown
🚘 <автомобиль>.

🔧 **Работы/задача:** <только важный список или итог>.

📦 **Запчасти:** <OEM/каталожный номер, наличие, поставщик или "не подобрано">.

💰 **Деньги:** **++<сумма или согласование>++**.

⚠️ *Важно:* <короткий риск, blocker, проверка или условие, если есть>.
```

Use only the blocks that matter for the card. For a tiny card, two paragraphs
are enough. Avoid decorative formatting, long source/provenance blocks, and
verbose AI explanations. Preserve technical data, part numbers, prices,
payments, contacts, diagnostics, files, and history exactly when they are still
relevant; if old text is noisy, reduce it to the important facts only.
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

### 1b. Repair Order Boundary

`Приберись` by itself does not authorize repair-order writes. During ordinary
cleanup, read repair orders when they explain money, work, materials, payment,
or completion state, then summarize the relevant facts in the card description
and board summary.

Only fill or rewrite a live repair order when the owner explicitly asks for
that target, for example `заполни заказ-наряд`, `распиши ЗН`, `добавь работы в
заказ-наряд`, or `обнови материалы в ЗН`. In that case:

- reread the card and repair order first
- preserve existing works, materials, prices, payments, comments, and dates
- write only confirmed work/material/payment rows from the owner's request or
  trusted source text
- verify the repair order after saving and report what changed

### 2. Vehicle Identity

If a card contains a VIN, chassis number, or body code:

- classify identifier type first
- use vehicle identity and VIN/OEM playbooks
- fill stable vehicle profile fields only
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
- deadline or follow-up point when useful
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
- set indicator/deadline/tag if useful
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
- when touching `description`, keep it short, recoverable, and readable:
  important facts only, split into useful paragraphs, not a full report, not a
  source log, and not a status/next-step instruction for the manager
- when rewriting `description`, actively apply supported rich text formatting
  and restrained emoji markers; use **bold**, *italic*, and ++underline++ for
  real emphasis, not decoration
- never write raw HTML or pseudo-formatting into a CRM description; visible
  markup is worse than no formatting
- do not add sources, long explanations, or extra background into the card
- after touching `description`, `title`, `tags`, or `vehicle_profile`, call
  `set_card_board_summary` and verify `board_summary_stale=false`
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

Indicator:

- red: payment blocker, urgent stale card, missing critical data, serious
  contradiction
- yellow: waiting for parts/client/diagnosis or a known non-urgent blocker
- green: ready/clear/no blocker

## Column Movement Boundary

Do not move or archive cards during board-cleanup autopilot, even when the next
operational state looks clear. Use non-moving, non-archive updates instead:

- tags
- indicators
- deadlines
- readable public description
- hidden `board_summary`
- short `AI:` note
- vehicle-profile enrichment
- archive recommendation in the final report

Move or archive a card only when the owner gives a separate explicit owner
command with the target card and requested destination or archive action.

## Final Report

Report to the owner briefly:

- cards checked
- cards updated
- cards archived=0 unless explicitly commanded
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
