# CRM Card Description Standard

Canonical style for public AutoStop CRM card `description` edits.

Use this standard whenever AutostopManager creates a card, updates a public
card description, writes parts/fluid/price results back to a card, or cleans an
existing non-empty description. It complements the task playbooks; it does not
replace their lookup, verification, write-boundary, or reread rules.

## Core Rule

The public card description is a minimum-sufficient working handoff, not a
research report and not a three-word label. It must let another employee
continue the job without reconstructing the current case from the full event
history. Prefer concise factual blocks, but never shorten away a confirmed
complaint, finding, agreed scope, parts state, result, or customer arrangement
that still affects the work.

## Must Do

- If the public description is empty, create it when confirmed working facts
  are available and the card needs a human-readable handoff. Leave it empty
  only when there is no supported operational content; never invent filler.
- Use the shortest text that remains operationally complete. A normal
  nontrivial active card should usually contain 4-10 short lines. A genuinely
  tiny one-step card may use 2-4 lines when no additional confirmed context
  affects the work.
- Include every applicable confirmed block: complaint/request, checked finding
  or diagnosis, agreed work, selected parts/materials and their current state,
  completed result, and current customer arrangement or follow-up point.
- Preserve useful staff-entered facts. Consolidate repetition, but do not
  replace a meaningful history-derived handoff with only a generic task name.
- Use CRM-supported Markdown only: `**bold**`, `*italic*`, `++underline++`.
- Use **bold** for labels and decisive facts.
- Use `++underline++` for the key catalog/OEM number, amount, oil capacity,
  approval, or money value when emphasis helps.
- Use *italic* only for a short secondary working note.
- Use restrained emoji markers only when they improve scanning.
- Keep only facts needed for the work: auto, complaint/request, checked
  finding, agreed work, selected part/material and its state, OEM/catalog
  number, oil/fluid capacity, spec, confirmed price, completed result, and
  compact customer arrangement or follow-up point.
- Put phone, VIN, plate, mileage, engine, gearbox, and drivetrain into
  structured CRM fields/vehicle_profile when possible. Keep them in public
  description only when the owner explicitly wants them visible there or the
  current intake still needs them operationally visible.

## Must Not Do

- Do not add risk/caveat/safety blocks.
- Do not use vague metadata-only text such as `Статус: в работе` or
  `Следующий шаг: продолжить`. A concrete current state or follow-up belongs in
  a factual block such as `**Проверено:**`, `**Согласовано:**`,
  `**Запчасти:**`, `**Результат:**` or `**Договорённость:**`.
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

Use only the blocks that matter, but include all blocks needed for handoff:

```markdown
🚘 **Авто:** <make/model, year/body only if useful>.

**Обращение:** <complaint or requested work>.

**Проверено:** <confirmed finding or diagnosis>.

**Согласовано:** <approved work scope>.

**Запчасти:** **++<selected article/OEM/catalog number>++**, <quantity and current parts state>.

**Масло/жидкости:** **++<capacity/spec>++**.

**Деньги:** **++<sum/agreement>++**.

**Результат:** <completed confirmed result>.

**Договорённость:** <current customer arrangement or follow-up point>.
```

For a genuinely tiny card, 2-4 lines are enough:

```markdown
**Задача:** **Замена масла ДВС**.
**Масло/фильтр:** **++6,5 л++**, фильтр **++A 271 180 05 09++**.
**Договорённость:** выполнить при текущем визите.
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

**Обращение:** замена масла в двигателе.

**Масло ДВС:** **++6,5 л++**.

**Согласовано:** масло и фильтр.

**Договорённость:** выполнить при текущем визите.
```

## Nontrivial Repair Handoff

```markdown
🚘 **Авто:** Toyota Camry.

**Обращение:** стук спереди на неровностях.

**Проверено:** люфт правой стойки стабилизатора.

**Согласовано:** замена обеих стоек.

**Запчасти:** заказаны, ожидаются 19 августа.

**Договорённость:** после поступления связаться с клиентом.
```

## Board Summary

`board_summary` is separate from public `description`: it is a 1-4 line board
preview, not a substitute for the complete working handoff. Keep it plain
text, without rich formatting, emoji decoration, source lists, phone, full
client identity, raw VIN, or long issue lists. Do not delete useful description
facts merely because a shorter version exists in `board_summary`.

## Write Flow

Before writing, read the current card and identify the exact card id. Use
`agent_entity_context`, build `prepare_action_contract`, preview with
`agent_board_workflow(operation="cleanup_card", mode="dry_run")`, then apply
with a unique idempotency key and reread through `agent_entity_context`.
