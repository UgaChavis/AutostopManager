# Knowledge Shelves

Human entrypoint for routing, file placement, deletion, and source-pack policy.

## Agent Loop

1. Run `agent-brief` or `prepare-context` for non-trivial operational work.
2. Run `knowledge-probe "<query>"` before broad file reads.
3. Open the returned `open_first` and source-of-truth files.
4. Use `knowledge-search --domain <best_domain>` only when the first file is
   not enough.
5. Read source packs only after the route card and playbook say they are
   relevant.
6. After durable docs/catalog/skill changes, run `knowledge-sync`,
   `knowledge-audit`, `annotations-audit`, and `skills-audit`.
7. Before deleting tracked docs or generated artifacts, run `cleanup-audit` and
   remove only items that satisfy the deletion rule below.

## Shelves

| Shelf | Domain | Open first |
| --- | --- | --- |
| Manager startup | `startup_and_identity` | `AGENTS.md` |
| Knowledge operations | `knowledge_intake` | `docs/agent/knowledge_shelves.md` |
| Board cleanup | `board_cleanup_autopilot` | `docs/agent/board_cleanup_autopilot_playbook.md` |
| Service management | `service_management` | `docs/agent/krasnoyarsk_service_management_playbook.md` |
| Store analytics | `store_analytics_reporting` | `docs/agent/store_analytics_playbook.md` |
| Gmail | `gmail_operations` | `docs/agent/gmail_workflow_playbook.md` |
| Business identity | `business_identity` | `docs/agent/business_identity_playbook.md` |
| Business documents | `business_documents` | `docs/agent/business_document_quality_playbook.md` |
| CRM VIN/OEM parts | `crm_vin_oem_parts_lookup` | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` |
| AutoStop App store | `store_management` | `docs/agent/store_management_playbook.md` |
| Vehicle identity/OEM | `vehicle_identity_and_oem` | `docs/agent/vehicle_identity_playbook.md` |
| Parts sourcing | `parts_sourcing` | `docs/agent/parts_search_playbook.md` |
| Labor pricing | `work_labor_pricing` | `docs/agent/work_labor_pricing_playbook.md` |
| Repair sources | `automotive_repair` | `docs/agent/automotive_repair_source_playbook.md` |
| BMW | `bmw_repair`, `bmw_f15_n63` | `docs/agent/bmw_repair_playbook.md` |
| Toyota GR Yaris | `toyota_gr_yaris` | `docs/agent/toyota_gr_yaris_playbook.md` |
| Fluids | `fluids` | `docs/agent/fluid_maintenance_playbook.md` |
| Transmissions | `transmission` | `docs/agent/transmission_playbook.md` |
| ECU programming | `ecu_calibration_programming` | `docs/agent/ecu_calibration_programming_playbook.md` |
| 3D printing CAD | `3d_printing_cad` | `docs/agent/3d_printing_cad_playbook.md` |
| Remote Codex access | `remote_codex_access` | `docs/agent/codex_home_pc_reverse_ssh.md` |
| Deployment | `deployment` | `docs/agent/deployment_runbook.md` |

## File Classes

- `active_control`: `AGENTS.md`, `README.md`, route maps, MCP catalogs,
  command routes, and startup rules.
- `active_playbook`: workflow instructions opened by route cards.
- `structured_catalog`: JSON/JSONL/CSV/YAML/SQL registries used by playbooks.
- `source_pack`: curated corpora under `source_cache/`; keep README/MANIFEST,
  source/license notes, and active structured tables.
- `reference_only`: audited supporting files that should not be broadly
  indexed.
- `delete_candidate`: fully migrated drafts, duplicate generated summaries,
  broken scratch files, obsolete instructions with no active route.
- `untracked_artifact`: runtime output, caches, SQLite, CRM evidence, generated
  PDFs; keep out of Git unless explicitly promoted.

## Placement Rules

- Put compact startup behavior in `AGENTS.md`.
- Prefer updating an existing canonical file over creating a new route file.
- Put detailed procedures in `docs/agent/*_playbook.md`.
- Put route metadata in `knowledge_map.json`.
- Put search summaries in `knowledge_annotations.jsonl`.
- Put source lists in `*_sources.json` or `*_catalog.json`.
- Put large model/source corpora under
  `docs/agent/automotive_sources/source_cache/<topic>_knowledge_pack/`.
- Put optional private runtime facts under `data/private_knowledge/`; never
  commit them.
- Use focused Codex skills only when a corpus should auto-trigger outside this
  repository; keep the project index linked to the skill, not duplicated.

## Route Card Contract

Every durable domain in `knowledge_map.json` should include:

- `title`
- `use_when`
- `aliases`
- `keywords`
- `questions`
- `source_of_truth_files`
- `primary_files`
- `reference_files` when a file must exist but stay link-only
- `optional_runtime_files` for private/local files that may be absent
- `required_context` when specific facts are needed before answering

Keep route cards compact. Put detailed procedure in playbooks and source packs.

## Annotation Contract

Each record in `knowledge_annotations.jsonl` should include stable
`annotation_id`, `domain`, `path`, `summary`, `use_when`, `keywords`,
`questions`, `source_type`, `trust_level`, `refresh_cadence`, and
`safety_flags`.

## Deletion Rule

Delete a tracked doc only when all are true:

1. Its active rule is already migrated into a playbook/catalog/index.
2. No route card, annotation, test, or README references it as source of truth.
3. It is not a required README/MANIFEST/source/license file for a source pack.
4. `knowledge-audit` and `annotations-audit` remain green after removal.

Do not keep a growing archive of obsolete plans. Prefer deletion after migration.

## Intake Checklist

1. Classify domain, source type, license, trust level.
2. Decide whether the file is source of truth, reference, or temporary evidence.
3. Place raw files in the smallest relevant shelf.
4. Extract durable rules into the relevant playbook/catalog.
5. Update `knowledge_map.json` and `knowledge_annotations.jsonl`.
6. Update the shelf table when a new durable route is added.
7. Run sync/audits and focused tests.

## Safety Boundaries

- Do not store full manuals, copied licensed databases, full spreadsheets, CRM
  snapshots, raw store orders/contacts/line items/stock rows/API payloads, raw
  Gmail threads, or temporary marketplace search results in manager memory.
- Do not move private business requisites from `data/private_knowledge/` into
  tracked docs.
- For repair, programming, immobilizer, SRS, HV, pinout, torque, or fluid facts,
  use source routing and verify with OEM/licensed/current sources when it
  affects a real vehicle.
- For parts listings, treat stock text as unconfirmed until API/cabinet/phone
  or message confirmation.

## Maintenance

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli skills-audit
python -m pytest tests/test_knowledge_base.py -q
```
