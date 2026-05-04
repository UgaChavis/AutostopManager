# Knowledge Shelves

Purpose: make the local knowledge base easy to route before reading full files.
This file is the shelf map: where knowledge belongs, what must be indexed, and
which instructions an agent should follow before using or adding material.

## Default Agent Loop

Use this loop before broad file reads:

1. Run `probe_knowledge_base` with the owner's natural request.
2. Open the returned `open_first` file when `has_knowledge=true`.
3. Use `search_knowledge_base --domain <best_domain>` only when the first file
   does not answer the task.
4. Read raw source packs only after the route card and source-of-truth file.
5. After adding or changing durable knowledge, run `knowledge-sync`, then
   `knowledge-audit`.

If `has_knowledge=false`, use external/OEM/current sources for the answer, then
decide whether the reusable route belongs in the intake flow.

## Current Shelves

| Shelf | Domain | Open First | Belongs Here |
| --- | --- | --- | --- |
| Manager behavior | `startup_and_identity` | `docs/agent/autostop_manager_skill.md` | startup routine, answer rules, memory boundaries, MCP catalogs |
| Knowledge operations | `knowledge_intake` | `docs/agent/knowledge_intake_playbook.md` | new files, indexing, shelf placement, route-card cleanup, source licensing |
| General repair sources | `automotive_repair` | `docs/agent/automotive_repair_source_playbook.md` | diagnostic source hierarchy, TSB/recall/wiring/torque/labor routes |
| ECU programming | `ecu_calibration_programming` | `docs/agent/ecu_calibration_programming_playbook.md` | ECU flashing, coding, calibration formats, UDS/J2534, BMW KOMBI/legal limits |
| BMW general repair | `bmw_repair` | `docs/agent/bmw_repair_playbook.md` | BMW diagnostics, DTC, xDrive, ZF, body electronics, HV, fluids |
| BMW F15/N63TU | `bmw_f15_n63` | `docs/agent/bmw_repair_playbook.md` | BMW X5 F15/F16 xDrive50i N63TU model-specific diagnostics |
| Toyota GR Yaris | `toyota_gr_yaris` | `docs/agent/toyota_gr_yaris_playbook.md` | GXPA16/G16E-GTS, GR-FOUR, EA67F, model-specific repair and fluids |
| Fluids | `fluids` | `docs/agent/fluid_maintenance_playbook.md` | oils, operating fluids, approvals, fill capacities, maintenance quantities |
| Transmissions | `transmission` | `docs/agent/transmission_playbook.md` | gearbox/CVT/DCT/AMT/clutch symptoms, adaptation, service routing |
| Vehicle identity/OEM | `vehicle_identity_and_oem` | `docs/agent/vehicle_identity_playbook.md` | VIN/frame classification, OEM lookup, catalog routing |
| Parts sourcing | `parts_sourcing` | `docs/agent/ai_parts_krasnoyarsk_playbook.md` | Krasnoyarsk parts search, ZZap/Drom/Avito, procurement price, offer scoring |
| Service management | `service_management` | `docs/agent/krasnoyarsk_service_management_playbook.md` | workshop triage, staff, finance, customer flow, board cleanup |
| Deployment | `deployment` | `docs/agent/deployment_runbook.md` | local MCP startup, server publishing, GitHub/private-data boundary |

## File Types And Placement

Use these locations consistently:

- `docs/agent/knowledge_base_index.md` - human entrypoint and short route list.
- `docs/agent/knowledge_map.json` - machine route cards for probe/search.
- `docs/agent/knowledge_shelves.md` - shelf map and placement rules.
- `docs/agent/*_playbook.md` - task workflow or domain operating procedure.
- `docs/agent/*_sources.json` - curated source catalogs and routing metadata.
- `docs/agent/automotive_sources/*` - automotive source catalogs and ingestion
  guidance.
- `docs/agent/automotive_sources/source_cache/<topic>_knowledge_pack/` - raw or
  owner-provided packs that should not be duplicated into memory.
- `C:/Users/9860606/.codex/skills/<topic>/` - optional focused trigger skills
  for large model-specific corpora when a local skill is actually installed.
- `data/` - local runtime storage, audit output, temporary evidence, and other
  material that should usually stay out of Git.

Do not move existing source packs just to make the tree prettier. Add route
metadata and README/MANIFEST coverage first; move files only when a source is
misclassified or unsafe in its current location.

## Naming Rules

- Use lowercase snake_case for durable project files.
- Use `<domain>_playbook.md` for workflow instructions.
- Use `<domain>_sources.json` or `<domain>_catalog.json` for structured source
  lists.
- Use `<topic>_knowledge_pack` for owner-provided corpora.
- Add `_ru` to pack documents when the primary human reading language is
  Russian.
- Prefer Markdown/JSON/JSONL/CSV for indexed text. Keep PDFs as source
  attachments and index their Markdown/plain-text counterpart when available.

## Route Card Contract

Every durable domain in `knowledge_map.json` should include:

- `title` - short human-readable domain name.
- `use_when` - the situations where this domain should win.
- `aliases` - Russian/English names, owner phrasing, typos, brand/model names.
- `keywords` - systems, components, source names, DTC words, workflow terms.
- `questions` - natural questions the route should answer.
- `source_of_truth_files` - the first files to open.
- `primary_files` - files that should be synced into SQLite search.
- `required_context` - facts the agent should collect before giving a specific
  answer.

Keep route cards compact. Put detailed procedure in playbooks and source packs,
not in `knowledge_map.json`.

## Source Pack Contract

A source pack should have at least:

- `README.md` or `README_ru.md` with purpose, load order, and safety limits.
- `MANIFEST.md` or `manifest.json` listing included files.
- `md/` or `markdown/` for searchable text equivalents of PDFs.
- `data/` for CSV/JSON/JSONL tables.
- `sources/` for citations, standards, licenses, and source catalogs.

Optional folders:

- `pdf/` for printable/source attachments.
- `examples/` for toy samples and safe fixtures.
- `prompts/` for reusable agent prompts.
- `schemas/` for JSON schemas.
- `configs/` for YAML/JSON configuration.
- `code_skeleton/` for reference implementation drafts.
- `openapi/` for API specs.

## Intake Checklist

When new knowledge arrives:

1. Classify the material by domain, source type, license, and trust level.
2. Decide whether it is source of truth, reference material, or temporary
   evidence.
3. Place raw files in the smallest relevant existing shelf.
4. Extract durable rules into the relevant playbook/catalog.
5. Add or update `knowledge_map.json` route-card fields.
6. Link the route from `knowledge_base_index.md` when a human should see it.
7. Run `python -m autostop_manager.cli knowledge-sync`.
8. Run `python -m autostop_manager.cli knowledge-audit`.
9. Add a focused test when the route should be recognized from owner phrasing.

## Safety Boundaries

- Do not store full manuals, copied licensed databases, full spreadsheets, CRM
  snapshots, raw email threads, or temporary marketplace search results in
  manager memory.
- For licensed/restricted sources, store only source routes and durable
  conclusions that are safe to reuse.
- For automotive repair, programming, immobilizer, SRS, HV, pinout, torque, or
  fluid-capacity facts, use source routing and verify with OEM/licensed/current
  sources when the answer affects a real vehicle.
- For parts listings, do not treat stock text as confirmed availability without
  API/cabinet/phone/message confirmation.

## Maintenance Commands

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli knowledge-probe "структурируй базу знаний разметка полки"
python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
python -m pytest tests/test_knowledge_base.py -q
```
