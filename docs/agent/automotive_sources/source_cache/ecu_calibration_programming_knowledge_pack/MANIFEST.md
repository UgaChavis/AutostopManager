# ECU Calibration Programming Pack Manifest

Purpose: list the files in this owner-provided source pack so agents can route
the material without reading every attachment first.

## Top Level

- `README.md` - purpose, safety boundary, and recommended reading order.
- `data/` - CSV/JSONL reference tables for scenario cards, risk checks, module
  dictionaries, and glossary terms.
- `sources/` - source catalog and public/legal reference routing.

## Load Order

1. `docs/agent/ecu_calibration_programming_playbook.md`
2. `README.md`
3. `data/`
4. `sources/`

Generated PDF duplicates were removed on 2026-05-08. Synthetic examples,
flashcards, and duplicate CSV glossary output were removed during the
documentation reduction pass. During the 2026-05-29 hard cleanup, long
Markdown modules were migrated into
`docs/agent/ecu_calibration_programming_playbook.md` and deleted. The retained
pack is README plus data/sources.

## Safety Boundary

This pack is route/index material for lawful diagnostics, repair validation,
calibration literacy, and OEM programming workflow. Do not use it as approval
to disable emissions systems, bypass immobilizer/security controls, alter
odometer data, or replace VIN-specific OEM service information.
