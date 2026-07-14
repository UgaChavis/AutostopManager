# CRM Card Description Standard

Canonical style for public AutoStop CRM card `description` edits.

Use this standard whenever AutostopManager creates a card, updates a public
card description, writes parts/fluid/price results back to a card, or cleans an
existing non-empty description. It complements the task playbooks; it does not
replace their lookup, verification, write-boundary, or reread rules.

## Core Rule

The public card description is a laconic working note, not a research report.
It should help the operator scan the card quickly.

## Must Do

- Keep the text as short as possible. For a simple card, 2-6 short lines are
  usually enough.
- Use CRM-supported Markdown only: `**bold**`, `*italic*`, `++underline++`.
- Use **bold** for labels and decisive facts.
- Use `++underline++` for the key catalog/OEM number, amount, oil capacity,
  approval, or money value when emphasis helps.
- Use *italic* only for a short secondary working note.
- Use restrained emoji markers only when they improve scanning.
- Keep only facts needed for the work: auto, task, selected part/material,
  OEM/catalog number, oil/fluid capacity, spec, price, agreement, and compact
  customer-visible arrangement.
- Put phone, VIN, plate, mileage, engine, gearbox, and drivetrain into
  structured CRM fields/vehicle_profile when possible. Keep them in public
  description only when the owner explicitly wants them visible there or the
  current intake still needs them operationally visible.

## Must Not Do

- Do not add risk/caveat/safety blocks.
- Do not write `Статус:` or `Следующий шаг:` headings.
- Do not write supplier-check reminders such as `проверить применимость`,
  `финально сверить у поставщика`, `требуется проверка`, or `перед заказом`.
- Do not include source/provenance/method text: where the part was found, how
  it was selected, confidence labels, supplier/source lists, search history, or
  long diagnostic theory.
- Do not write AI-style explanations.
- Do not paste raw scans, long private excerpts, full client identity, or raw
  VIN dumps into description.
- Do not use raw HTML tags, pseudo-formatting, or markup that remains visible
  in the CRM preview.

## Default Shape

Use only the blocks that matter:

```markdown
🚘 **Авто:** <make/model, year/body only if useful>.

**Задача:** **<short work essence>**.

**Запчасти:** **++<selected article/OEM/catalog number>++**, <qty/price if known>.

**Масло/жидкости:** **++<capacity/spec>++**.

**Деньги:** **++<sum/agreement>++**.
```

For a tiny card, one or two lines are enough:

```markdown
**Задача:** **Замена масла ДВС**.
**Масло/фильтр:** **++6,5 л++**, фильтр **++A 271 180 05 09++**.
```

## Parts/OEM Result

```markdown
🚘 **Авто:** Mercedes-Benz E200.

**Задача:** **Масляный фильтр ДВС**.

**Каталожный номер:** **++A 271 180 05 09++**.
```

## Service Intake

```markdown
🚘 **Авто:** Mercedes-Benz E200.

**Задача:** **Замена масла в двигателе**.

**Масло ДВС:** **++6,5 л++**.
```

## Board Summary

`board_summary` is separate from public `description`. Keep it plain text,
without rich formatting, emoji decoration, source lists, phone, full client
identity, raw VIN, or long issue lists. Use 1-4 short lines.

## Write Flow

Before writing, read the current card and identify the exact card id. Use
`agent_entity_context`, build `prepare_action_contract`, preview with
`agent_board_workflow(operation="cleanup_card", mode="dry_run")`, then apply
with a unique idempotency key and reread through `agent_entity_context`.
