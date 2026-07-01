# Knowledge Base Index

Compact human navigation for AutostopManager knowledge. Use route cards and
playbooks instead of broad-reading the tree.

## Fast Route

1. For a new manager task, run `agent-brief` or `prepare-context`.
2. For local knowledge, run `knowledge-probe "<query>"`.
3. If `has_knowledge=true`, open returned `open_first` / source-of-truth files.
4. If more detail is needed, run `knowledge-search --domain <best_domain>`.
5. If adding or moving durable knowledge, update `knowledge_map.json`,
   `knowledge_annotations.jsonl`, and the smallest relevant playbook/catalog.
6. Keep raw CRM/Gmail/private evidence out of Git and durable memory.

## Core Control Files

- `AGENTS.md` - canonical compact startup instruction for Codex.
- `docs/agent/autostop_manager_skill.md` - detailed startup behavior and route
  explanations.
- `docs/agent/manager_rules.json` - durable operating rules with priorities.
- `docs/agent/command_routes.json` - standing owner-command aliases.
- `docs/agent/crm_card_description_standard.md` - public CRM card
  description create/update style.
- `docs/agent/knowledge_shelves.md` - shelf map, placement, deletion policy.
- `docs/agent/codex_home_pc_reverse_ssh.md` - current `home-pc` reverse SSH
  route, toolset, and helper workflow from this server to the owner's Windows
  PC.
- `docs/agent/knowledge_map.json` - machine route cards.
- `docs/agent/knowledge_annotations.jsonl` - compact file annotations.
- `docs/agent/manager_mcp_catalog.json` - AutostopManager MCP tool surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM connector surface.
- `docs/agent/gmail_workflow_playbook.md` and
  `docs/agent/gmail_mcp_catalog.json` - Gmail workflow and tool surface.
- `docs/agent/crm_manager_data_playbook.md` - safe CRM manager summaries.

## Route Table

| Domain | Open first | Use when |
| --- | --- | --- |
| `startup_and_identity` | `AGENTS.md` | Startup, answer style, memory boundaries, command routing. |
| `knowledge_intake` | `docs/agent/knowledge_shelves.md` | New files, docs cleanup, indexing, source-pack policy. |
| `board_cleanup_autopilot` | `docs/agent/board_cleanup_autopilot_playbook.md` | Owner says `Приберись`; no card movement/archive/order/payment writes without separate command. |
| `crm_card_description_standard` | `docs/agent/crm_card_description_standard.md` | Creating, updating, or cleaning public CRM card descriptions; laconic rich text without sources/provenance, risk blocks, or supplier-check reminders. |
| `service_management` | `docs/agent/krasnoyarsk_service_management_playbook.md` | Daily control, ready unpaid, staff/load, customer flow, finance, procurement blockers. |
| `gmail_operations` | `docs/agent/gmail_workflow_playbook.md` | Gmail search, labels, drafts, attachments, thread reads, safe mailbox changes. |
| `business_identity` | `docs/agent/business_identity_playbook.md` | Private ИП/AutoStop requisites route; exact facts live in ignored private files. |
| `business_documents` | `docs/agent/business_document_quality_playbook.md` | PDF/DOCX/XLSX, счет, акт, КП, requisites, render QA. |
| `crm_vin_oem_parts_lookup` | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` | CRM card VIN/frame -> OEM -> crosses -> prices -> CRM writeback. |
| `vehicle_identity_and_oem` | `docs/agent/vehicle_identity_playbook.md` | VIN/frame classification, market identity, OEM lookup route. |
| `parts_sourcing` | `docs/agent/ai_parts_krasnoyarsk_playbook.md` | Drom/ZZap/Avito/supplier search, Krasnoyarsk availability, offer scoring. |
| `work_labor_pricing` | `docs/agent/work_labor_pricing_playbook.md` | Read-only labor estimates from public Russia STO samples plus AutoStop +50%. |
| `automotive_repair` | `docs/agent/automotive_repair_source_playbook.md` | Diagnostics, TSB/recall/wiring/torque/labor/source routing. |
| `bmw_repair` / `bmw_f15_n63` | `docs/agent/bmw_repair_playbook.md` | BMW diagnostics, xDrive, ZF, body electronics, F15/N63 route. |
| `toyota_gr_yaris` | `docs/agent/toyota_gr_yaris_playbook.md` | GXPA16/G16E-GTS/GR-FOUR model-specific route. |
| `fluids` | `docs/agent/fluid_maintenance_playbook.md` | Oils, fluids, approvals, fill capacities, service-fill workflow. |
| `transmission` | `docs/agent/dsg_transmission_playbook.md` | Gearbox/CVT/DCT/AMT/clutch, DSG/S tronic, adaptation, ODIS/SVM. |
| `ecu_calibration_programming` | `docs/agent/ecu_calibration_programming_playbook.md` | ECU flash/coding/calibration, UDS/J2534, BMW KOMBI/legal limits. |
| `3d_printing_cad` | `docs/agent/3d_printing_cad_playbook.md` | CAD/STL/Anycubic Kobra S1 workflow. |
| `deployment` | `docs/agent/deployment_runbook.md` | Publishing, GitHub/private-data boundary, verification order. |
| `remote_codex_access` | `docs/agent/codex_home_pc_reverse_ssh.md` | Current `ssh home-pc`, `sftp`/`scp`, `pwsh`, and Python access to the owner's Windows PC through reverse SSH. |

## Business Documents Rule

AutoStop service documents are routed through the CRM print module and standard
AutoStop templates. Use `download_repair_order_print_pdf` for documents with
CRM cards and `create_document_without_card_pdf` for "Документ без карточки".
Pass `tax_label` such as `Без НДС` through CRM when needed. Do not build
independent PDF/HTML templates for AutoStop invoices, acts, repair orders,
invoice-facturas, defect reports, completion acts, or parts-sale documents.
The CRM client can infer the standard document type from Russian request text.

## Source Pack Policy

Large source packs are cold references. Keep README/MANIFEST, source/license
notes, and important CSV/JSON/JSONL tables. Delete generated duplicate chapters
only after unique rules have moved into canonical playbooks/catalogs and
`knowledge-audit` remains green.

## Maintenance Commands

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "проверить Gmail коннектор почта ярлыки вложения"
python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli system-audit
```

Use `rg` for local checks. Search scoped playbooks/catalogs before opening raw
source packs.
