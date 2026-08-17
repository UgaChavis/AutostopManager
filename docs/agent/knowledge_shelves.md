# Knowledge Shelves

Human entrypoint for routing, placement and deletion. Run `agent-brief` for
non-trivial work, then `knowledge-probe`; open its `open_first`/source files
before broad reads. Route cards guide source selection but never force CRM,
Store or web access when the question does not need them.

## Shelves

| Shelf | Domain | Open first |
|---|---|---|
| Ecosystem parity | `ecosystem_capability_parity` | `AGENTS.md` |
| Manager startup | `startup_and_identity` | `AGENTS.md` |
| Learning | `intelligent_agent_learning` | `docs/agent/intelligent_agent_learning_playbook.md` |
| Knowledge operations | `knowledge_intake` | `docs/agent/knowledge_shelves.md` |
| Board cleanup | `board_cleanup_autopilot` | `docs/agent/board_cleanup_autopilot_playbook.md` |
| Service management | `service_management` | `docs/agent/krasnoyarsk_service_management_playbook.md` |
| Store/analytics *(paused until explicit reauthorization)* | `store_management`, `store_analytics_reporting` | `docs/agent/store_management_playbook.md` |
| Gmail | `gmail_operations` | `docs/agent/gmail_workflow_playbook.md` |
| Telegram | `telegram_operations` | `.agents/skills/manage-owner-telegram/SKILL.md` |
| Business identity/documents | `business_identity`, `business_documents` | `docs/agent/business_document_quality_playbook.md` |
| CRM VIN/OEM writeback | `crm_vin_oem_parts_lookup` | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` |
| Vehicle/OEM and parts | `vehicle_identity_and_oem`, `parts_sourcing` | `docs/agent/vehicle_identity_playbook.md` |
| Labor pricing | `work_labor_pricing` | `docs/agent/work_labor_pricing_playbook.md` |
| General repair | `automotive_repair` | `docs/agent/automotive_repair_source_playbook.md` |
| BMW | `bmw_repair`, `bmw_f15_n63` | `docs/agent/bmw_repair_playbook.md` |
| Toyota GR Yaris | `toyota_gr_yaris` | `docs/agent/toyota_gr_yaris_playbook.md` |
| Fluids/transmissions | `fluids`, `transmission` | `docs/agent/fluid_maintenance_playbook.md` |
| ECU programming | `ecu_calibration_programming` | `docs/agent/ecu_calibration_programming_playbook.md` |
| 3D printing | `3d_printing_cad` | `docs/agent/3d_printing_cad_playbook.md` |
| Server/remote PCs | `remote_codex_access` | `docs/agent/codex_home_pc_reverse_ssh.md` |
| Public city cameras | `public_camera` | `.agents/skills/capture-public-camera/SKILL.md` |
| Deployment | `deployment` | `docs/agent/deployment_runbook.md` |

Use `knowledge-search --domain <domain>` only when the first file is
insufficient. Open source packs only when their route/playbook calls for them.

## Placement

- `AGENTS.md`: compact startup rules.
- `*_playbook.md`: one canonical detailed workflow.
- `knowledge_map.json`: compact route cards.
- `knowledge_annotations.jsonl`: short searchable file summaries.
- `*_sources.json` / `*_catalog.json`: structured source registries.
- `source_cache/<topic>_knowledge_pack/`: curated corpora with required
  README/MANIFEST, source/license notes and active tables.
- `data/private_knowledge/`: optional private runtime facts, never Git.

Prefer updating an existing canonical file over creating a new route. Link to
an owned procedure instead of copying it. Runtime output, caches, SQLite, CRM
evidence and generated documents remain untracked.

Each route card needs a stable title, recognition terms, source-of-truth and
primary files; add reference/optional files and required context only when
needed. Each annotation needs stable id, domain, path, compact summary,
recognition terms, source type and trust level; safety flags are optional.

## Deletion Rule

Delete a tracked doc only when all are true:

1. Unique active rules are migrated to a canonical owner.
2. Routes, annotations, tests and README no longer depend on it.
3. It is not a required source-pack manifest/license/source file.
4. `cleanup-audit`, knowledge and annotation audits remain green.

Do not keep an active archive of migrated plans or copied source material.
Never promote private records, raw manuals/databases, CRM/Store/Gmail dumps or
temporary marketplace evidence into Manager documentation.

After durable docs/catalog/skill changes run `knowledge-sync`,
`knowledge-audit`, `annotations-audit`, `skills-audit` and focused tests. For
repair, programming, safety, torque, fluid or fitment facts, route to current
OEM/licensed evidence rather than treating the local index as authority.
