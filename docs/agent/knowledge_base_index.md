# Knowledge Base Index

Purpose: compact entry point for AutostopManager knowledge. Use this file to
choose the right source-of-truth document, then read only what the task needs.

## Fast Route

1. Run `agent_brief` for a new manager task.
2. Run `probe_knowledge_base` or
   `python -m autostop_manager.cli knowledge-probe "<query>"`.
3. If `has_knowledge=true`, open the returned `open_first` /
   `source_of_truth` file before broad reads.
4. Use `reference_files` only for linked manifests, schemas, source catalogs,
   or cold source-pack evidence the task specifically needs.
5. If more detail is needed, run `search_knowledge_base` inside the returned
   domain.
6. If the route is missing, answer from current/OEM/source-backed material, then
   decide whether durable knowledge belongs in intake.

Search examples assume `rg`; if unavailable, use `grep -RIn` with the same
patterns and a scoped path.

## Local Commands

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "Приберись"
python -m autostop_manager.cli knowledge-probe "проверить Gmail почта ярлыки вложения"
python -m autostop_manager.cli knowledge-probe "сделать счет PDF НДС реквизиты"
python -m autostop_manager.cli knowledge-search "рейка Красноярск offer scoring" --domain parts_sourcing
python -m autostop_manager.cli knowledge-search "A2L DCM ODX" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli system-audit
```

## Control Files

- `autostop_manager_skill.md` - role, startup loop, memory boundaries.
- `command_routes.json` - owner phrase to route mapping.
- `knowledge_shelves.md` - shelf map, deletion rules, source-pack policy.
- `knowledge_map.json` - machine route cards.
- `knowledge_annotations.jsonl` - compact searchable file annotations.
- `manager_mcp_catalog.json` - local manager MCP tools.
- `crm_mcp_catalog.json` - AutoStop CRM MCP tools.
- `gmail_workflow_playbook.md` and `gmail_mcp_catalog.json` - Gmail workflow
  and tool catalog.
- `business_document_quality_playbook.md` - PDF/DOCX/XLSX quality gate.

## Active Domains

| Domain | Open First | Use When |
| --- | --- | --- |
| `startup_and_identity` | `autostop_manager_skill.md` | agent startup, memory, operating rules |
| `knowledge_intake` | `knowledge_intake_playbook.md` | new files, docs cleanup, route/index changes |
| `business_identity` | `business_identity_playbook.md` | current private ИП/AutoStop реквизиты route |
| `business_documents` | `business_document_quality_playbook.md` | PDF, DOCX, XLSX, счета, акты, КП |
| `gmail_operations` | `gmail_workflow_playbook.md` | Gmail search, labels, drafts, attachments, send/archive safety |
| `board_cleanup_autopilot` | `board_cleanup_autopilot_playbook.md` | `Приберись`, card cleanup, vehicle passport, formatted descriptions |
| `service_management` | `krasnoyarsk_service_management_playbook.md` | workshop daily control, ready/unpaid, staff, finance, customers |
| `work_labor_pricing` | `work_labor_pricing_playbook.md` | read-only labor estimates and public norm-hour plausibility |
| `vehicle_identity_and_oem` | `vin_oem_lookup_playbook.md` | VIN/chassis identity and OEM number dossiers |
| `parts_sourcing` | `ai_parts_krasnoyarsk_playbook.md` | Красноярск parts search, suppliers, Drom/ZZap/Avito, offer scoring |
| `automotive_repair` | `automotive_repair_source_playbook.md` | generic repair source routing, TSB, recall, wiring, torque |
| `bmw_repair` | `bmw_repair_playbook.md` | BMW diagnostics, DTC, xDrive, ZF, electronics, fluids |
| `bmw_f15_n63` | `bmw_repair_playbook.md` | BMW F15/F16 N63TU-specific route |
| `toyota_gr_yaris` | `toyota_gr_yaris_playbook.md` | GR Yaris/GXPA16/G16E-GTS |
| `ecu_calibration_programming` | `ecu_calibration_programming_playbook.md` | ECU flashing, coding, calibration, KOMBI, legal boundaries |
| `transmission` | `transmission_playbook.md` | transmissions, DSG/S tronic, adaptations |
| `fluids` | `fluid_maintenance_playbook.md` | oils, approvals, fill quantities |
| `obsidian_knowledge_vault` | `obsidian_knowledge_vault_playbook.md` | Obsidian working vault and safe summaries |
| `deployment` | `deployment_runbook.md` | local/server startup and publishing |

## High-Value Routes

### Приберись

Open `board_cleanup_autopilot_playbook.md`. Fill vehicle passport fields from
available source-backed data, rewrite the public description with useful
paragraphs, emoji markers, and CRM Markdown, keep `board_summary` plain, and do
not move/archive cards or write repair orders unless the owner separately asks
for that exact action.

### Gmail

Open `gmail_workflow_playbook.md`. Search/read exact messages before decisions.
Preview bulk queries before mailbox mutations. Sending, forwarding, archiving,
deleting, labelling, or draft updates require explicit owner approval.

### Business Documents

Open `business_document_quality_playbook.md`. Use `business_identity` only for
current private company facts. Verify реквизиты, number/date, totals, НДС,
signature/stamp blocks, and render every page/sheet before delivery.

### Automotive Source Packs

`docs/agent/automotive_sources/source_cache/` is cold reference material. Active
rules belong in playbooks and structured catalogs; source packs should retain
only README/MANIFEST, source/license notes, and important CSV/JSON/JSONL tables.
Do not re-expand deleted draft chapters unless a current task and tests require
them.

## Update Checklist

When adding or reorganizing durable knowledge:

1. Classify the material by domain, source type, license, and trust level.
2. Move active procedure into the smallest relevant playbook/catalog.
3. Update `knowledge_map.json` and `knowledge_annotations.jsonl`.
4. Keep `knowledge_base_index.md` as navigation only.
5. Delete fully migrated duplicates instead of growing an archive.
6. Run `knowledge-sync`, `knowledge-audit`, `annotations-audit`,
   `cleanup-audit`, `system-audit`, and focused pytest.
