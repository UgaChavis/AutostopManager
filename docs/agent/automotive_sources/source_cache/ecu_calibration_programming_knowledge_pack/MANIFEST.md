# ECU Calibration Programming Pack Manifest

Purpose: list the files in this owner-provided source pack so agents can route
the material without reading every attachment first.

## Top Level

- `README.md` - purpose, safety boundary, and recommended reading order.
- `md/` - searchable Markdown modules used by the knowledge index.
- `data/` - CSV/JSONL reference tables for scenario cards, risk checks, module
  dictionaries, and glossary terms.
- `sources/` - source catalog and public/legal reference routing.

## Load Order

1. `README.md`
2. `md/00_scope_and_boundaries_ru.md`
3. `md/01_ecu_fundamentals_ru.md`
4. `md/02_networks_uds_obd_ru.md`
5. `md/03_file_formats_ru.md`
6. `md/04_calibration_theory_ru.md`
7. `md/05_oem_programming_workflow_ru.md`
8. `md/06_bmw_programming_overview_ru.md`
9. `md/07_emissions_diagnostics_ru.md`
10. `md/08_flash_failures_recovery_ru.md`
11. `md/09_validation_logging_ru.md`
12. `md/10_service_scenarios_ru.md`
13. `md/99_index_ru.md`
14. `data/`
15. `sources/`

Generated PDF duplicates were removed on 2026-05-08. Synthetic examples,
flashcards, and duplicate CSV glossary output were removed during the
documentation reduction pass. The active indexed source is Markdown plus
data/sources.

## Safety Boundary

This pack is route/index material for lawful diagnostics, repair validation,
calibration literacy, and OEM programming workflow. Do not use it as approval
to disable emissions systems, bypass immobilizer/security controls, alter
odometer data, or replace VIN-specific OEM service information.
