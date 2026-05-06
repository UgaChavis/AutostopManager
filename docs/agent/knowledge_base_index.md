# Knowledge Base Index

Purpose: one entry point for AutostopManager knowledge. Start here when the owner asks to expand, search, audit, or use the local knowledge base.

## Fast Route

1. Run `probe_knowledge_base` or `python -m autostop_manager.cli knowledge-probe "<query>"`.
2. If `has_knowledge=true`, open `open_first` / `source_of_truth` before broad file reads. Use returned `reference_files` only when the task specifically asks for a linked pack artifact, schema, manifest, source catalog, or implementation draft.
3. If more detail is needed, run `search_knowledge_base` inside the returned `best_domain`.
4. If `has_knowledge=false`, route to external/OEM sources and consider knowledge intake after the answer.
5. If the task is technical automotive work, collect VIN/chassis, market, engine, transmission, mileage, complaint, and scan results before giving facts.
6. If adding new knowledge, follow `knowledge_intake_playbook.md`, update this index plus `knowledge_map.json`, then run `sync_knowledge_base`.
7. Keep raw evidence out of memory and Git unless explicitly safe. Store durable rules, source routes, and short verified conclusions.

## Indexed Navigation

Use these commands when working locally:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "BMW X5 кузов E15 мотор N63 электрика"
python -m autostop_manager.cli knowledge-probe "подобрать сцепление Toyota Yaris GR"
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
python -m autostop_manager.cli knowledge-probe "сделать счет или КП в PDF Word Excel и проверить оформление"
python -m autostop_manager.cli knowledge-probe "проверить Gmail коннектор почта ярлыки вложения черновики"
python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "рейка Красноярск vendor discovery offer scoring call confirmation" --domain parts_sourcing
python -m autostop_manager.cli knowledge-search "счет акт КП НДС реквизиты render audit" --domain business_documents
python -m autostop_manager.cli knowledge-search "search_emails labels drafts attachments write safety" --domain gmail_operations
python -m autostop_manager.cli knowledge-search "engine oil capacity" --domain fluids
python -m autostop_manager.cli knowledge-search "BMW xDrive shudder transfer case" --domain bmw_repair
python -m autostop_manager.cli knowledge-search "BMW F15 N63 BDC"
python -m autostop_manager.cli knowledge-search "GR Yaris G16E"
python -m autostop_manager.cli knowledge-audit
```

Use these MCP tools when working through the manager:

- `prepare_manager_context` - combine command routes, relevant memory/rules,
  knowledge routing, missing required context, and next actions.
- `sync_knowledge_base` - refresh the SQLite index after docs/catalog/skill changes.
- `probe_knowledge_base` - cheaply decide whether local knowledge exists and which source-of-truth file to open first.
- `search_knowledge_base` - find the right route before reading full files.
- `audit_knowledge_base` - verify route cards, mapped files, and index counts after source intake.
- `audit_knowledge_annotations` - verify compact sidecar annotations used for
  fast file-level routing before broad section reads.
- `audit_skill_registry` - verify linked local Codex skills exist and are
  mapped to knowledge domains.

## Route Cards

`knowledge_map.json` is not only a list of files. Each domain should carry a
compact route card:

- `aliases` - Russian/English names, common typos, model names, brand names, and owner phrasing.
- `keywords` - technical systems, components, fluids, DTC terms, and workflows.
- `questions` - natural-language questions the domain should answer.
- `source_of_truth_files` - the first files to open when the route matches.
- `primary_files` - files synced into SQLite full-text search.
- `reference_files` - active link-only files that are audited but not fully
  indexed because they are large, duplicated, or mainly bibliography/source-pack
  material.
- `optional_files` - local runtime/private files indexed only when present.

The agent should use route cards as the cheap first pass. Full document search
is the second pass. `reference_files` should remain visible in probe output but
should not produce full section matches in normal search.

## Core Control Files

- `autostop_manager_skill.md` - agent startup routine, role, memory boundaries, and canonical behavior.
- `manager_rules.json` - durable operating rules with priorities.
- `operating_playbook.json` - machine-readable startup and routing map.
- `knowledge_shelves.md` - shelf map, file placement rules, route-card contract, and maintenance checklist.
- `manager_mcp_catalog.json` - local AutostopManager MCP tool surface.
- `crm_mcp_catalog.json` - AutoStop CRM connector tool surface.
- `gmail_workflow_playbook.md` - Gmail workflow, read/write safety, query
  patterns, attachment caveats, and memory boundaries.
- `gmail_mcp_catalog.json` - Gmail connector tool surface and 2026-05-05
  read-only audit notes.
- `memory_policy.json` - memory storage boundaries and retention behavior.
- `command_routes.json` - canonical natural-language owner command aliases,
  open-first files, memory queries, and next actions.
- `knowledge_annotations.jsonl` - compact file-level annotations with domain,
  summary, keywords, source type, refresh cadence, safety flags, and related
  skills for fast routing.

## Knowledge Intake

- `knowledge_intake_playbook.md` - required workflow for new files, links, PDFs, spreadsheets, scans, catalogs, and owner notes.
- `knowledge_shelves.md` - where durable knowledge belongs, how route cards are marked, and how source packs should be signed.
- `automotive_sources/ingestion_tasks.jsonl` - pending/source-ingestion actions.
- `automotive_sources/db_schema.sql` - proposed future repair knowledge database schema.
- `business_identity_playbook.md` - private route for current ИП/AutoStop
  requisites and freshness sorting of owner business documents.
- `business_document_quality_playbook.md` - route for high-quality PDF/DOCX/XLSX
  business documents, invoices, acts, КП, totals, НДС wording, render QA, and
  audit before delivery or CRM upload.

Use intake when the owner says: "обнови базу знаний", "сохрани себе", "дополни инструкции", "запомни источник", "структурируй материалы", or provides files/links.

## Automotive Technical Knowledge

- `automotive_repair_source_playbook.md` - source routing and confidence rules for diagnostics, wiring, TSBs, recalls, repair procedures, fluids, torque, labor, ADAS, SRS, HV, immobilizer, keys, and programming.
- `automotive_sources/automotive_repair_sources_catalog.json` - main authoritative source catalog.
- `automotive_sources/brand_source_map.json` - preferred sources by vehicle brand.
- `automotive_sources/data_type_source_map.json` - preferred sources by technical data type.
- `automotive_sources/model_source_overrides.json` - model-specific routes such as BMW X5 F15/N63TU.
- `automotive_sources/open_dataset_endpoints.json` - legally open datasets.
- `fluid_maintenance_playbook.md` - oils, fluids, approvals, fill capacities, and service-fill workflow.
- `automotive_sources/fluid_maintenance_sources.json` - source routing for fluids and capacities.
- `transmission_playbook.md` - gearbox/CVT/DCT/AMT/clutch/transmission-fluid diagnostics and service workflow.
- `dsg_transmission_playbook.md` - Volkswagen Group DSG / Audi S tronic route for DQ/DL families, mechatronic modules, ODIS/SVM software updates, basic settings, adaptation, and used-unit sourcing risks.
- `automotive_sources/dsg_transmission_sources.json` - official/public DSG/S tronic source registry: erWin/SVM, VW/Audi NHTSA TSBs, and VW technology references.
- `ecu_calibration_programming_playbook.md` - ECU programming, calibration, coding, adaptation, UDS/OBD, file formats, BMW programming workflow, KOMBI/instrument-cluster, and "стрелковка" route.
- `automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/` - owner-provided ECU calibration/programming reference pack with Markdown/PDF/data/source indexes and synthetic examples.
- `bmw_repair_playbook.md` - general BMW diagnostics, DTC, chassis, body electronics, xDrive, ZF transmission, HV, and fluids route.
- `automotive_sources/source_cache/bmw_repair_knowledge_pack/` - owner-provided BMW repair reference pack with Markdown/PDF/data indexes.
- `toyota_gr_yaris_playbook.md` - Toyota GR Yaris / GXPA16 / G16E-GTS model-specific repair, fluids, recalls, TSB, and OEM parts routing.

DSG / S tronic route:

- `docs/agent/dsg_transmission_playbook.md`
- `docs/agent/automotive_sources/dsg_transmission_sources.json`
- `docs/agent/transmission_playbook.md`

For Volkswagen/Audi/Skoda/SEAT DSG or Audi S tronic requests, search
`transmission` first and open `dsg_transmission_playbook.md` before giving
software, adaptation, mechatronic, fluid, or parts-fitment guidance. Treat OEM
ODIS/SVM/TPI/TSB software updates as VIN/software-level-specific repair actions,
separate from TCU tuning/remap. For DQ200/DQ250/DQ381/DQ500/DL501/DL382 or
0AM/0CW/02E/0D9/0GC/0BH/0BT/0DL/0B5/0CK/0CL, require transmission code,
TCM/J743/J217 hardware/software numbers, DTCs, battery/power condition, and
service history before final recommendations.

ECU calibration/programming pack:

- `docs/agent/ecu_calibration_programming_playbook.md`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/README.md`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/md/`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/sources/`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/examples/`

For ECU programming, calibration formats, UDS/OBD/J2534, flash recovery, BMW ISTA/I-level/VO/FA, or "стрелковка" / KOMBI / instrument-cluster questions, search `ecu_calibration_programming` first. Treat cluster/needle requests as legal coding/adaptation diagnostics only; if the request touches odometer, VIN, EEPROM/NVM dump cloning, immobilizer, security bypass, or emissions delete, route to official/legal service procedure and do not provide bypass steps.

General BMW repair pack:

- `docs/agent/bmw_repair_playbook.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/README_ru.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/markdown/`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/data/`

For generic BMW diagnostics, DTC/fault memory, xDrive, ZF transmission,
electronics, HV, or fluids questions, search `bmw_repair` first; then narrow to
the BMW F15/N63 route when the vehicle matches.

BMW X5 F15/N63TU route:

- `docs/agent/bmw_repair_playbook.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/README_ru.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/markdown/`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/data/`

For BMW F15/N63 requests, load the BMW route first, then route safety-critical or VIN-specific facts through ISTA/AIR/ETK or BMW official sources.

Toyota GR Yaris route:

- `docs/agent/toyota_gr_yaris_playbook.md`

For Toyota GR Yaris / Yaris GR / GXPA16 / G16E-GTS requests, load the
playbook first, then verify VIN/frame-specific repair, TSB, wiring, torque,
fluid, recall, and OEM part facts through Toyota official or licensed sources.

Fluid maintenance has a dedicated playbook:

- `docs/agent/fluid_maintenance_playbook.md`
- `docs/agent/automotive_sources/fluid_maintenance_sources.json`

For oil, fluid, approval, and fill-capacity requests, load the playbook first,
then verify exact vehicle/unit data through OEM or licensed service sources.

## Vehicle Identity and OEM Parts

- `vehicle_identity_playbook.md` - classify VIN, Japanese frame/chassis number, Korean VIN, and market-specific codes.
- `vin_oem_lookup_playbook.md` - original catalog number lookup routing.
- `vin_oem_sources.json` - VIN/OEM source catalog.
- `parts_search_playbook.md` - Drom/marketplace sourcing workflow.
- `zzap_search_playbook.md` - ZZap price comparison, replacements, and local-region checks.
- `procurement_pricing_playbook.md` - закупочная цена, Красноярск-first availability, selected-part vs OEM-reference separation, package/unit math, and CRM material-total rules.
- `procurement_price_sources.json` - supplier/API catalog for ROSSKO/Роска, Armtek, Autopiter, AutoEuro, ZZap, AutoSputnik, APEC, PartsAPI, UMAPI, AUTOPOISK, Mikado, local Krasnoyarsk sources, access modes, and quote fields.
- `ai_parts_krasnoyarsk_playbook.md` - AI parts search, local Красноярск vendor discovery, offer scoring, seller-call confirmation, schemas, source registry, OpenAPI, and high-risk parts such as рулевая рейка.
- `automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/` - owner-provided AI Parts Search Krasnoyarsk/Russia project pack with docs, prompts, schemas, configs, data, code skeleton, and OpenAPI draft.

AI parts Красноярск project pack:

- `docs/agent/ai_parts_krasnoyarsk_playbook.md`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/README.md`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/docs/`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/prompts/`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/schemas/`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/configs/`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/data/`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/openapi/`

For spare-parts search, steering rack / рулевая рейка, used/contract parts,
seller discovery, call confirmation, offer scoring, API integration, or local
Красноярск availability questions, search `parts_sourcing` first. Do not treat
marketplace listings or supplier-site stock text as confirmed availability
without API/cabinet/phone/message confirmation.

## Business Identity

- `business_identity_playbook.md` - private local route for current ИП
  requisites, company-card data, AutoStop commercial-offer identity, and
  document freshness decisions.
- `data/private_knowledge/business_identity_current.json` - private current
  facts selected from the newest reliable documents. This file is local runtime
  knowledge and must not be committed.
- `data/private_knowledge/business_documents_inventory.json` - private
  filesystem inventory of the owner's synced document folder, with dates,
  hashes, and topic flags. This optional runtime file may be absent in a clean
  Git checkout.

For ИП / реквизиты / карточка предприятия / ИП Гришкявичус or Гришкевичус
requests, search `business_identity` first and use the private current JSON.
Before external use, verify exact banking/legal wording against the original
source document if formatting matters.

## Business Documents

- `business_document_quality_playbook.md` - AutoStop route for PDF/DOCX/XLSX
  invoices, acts, КП, receipts, requisites sheets, accounting-style documents,
  and printable forms.

For счет / акт / КП / бухгалтерский документ / PDF / Word / Excel requests,
search `business_documents` first. Use `business_identity` only for current
private company facts, then render-inspect the final artifact and run the
business-document audit script before saying the file is ready.

## Gmail Operations

- `gmail_workflow_playbook.md` - operational route for Gmail search, inbox
  triage, labels, drafts, attachments, thread reading, write safety, and memory
  extraction.
- `gmail_mcp_catalog.json` - current discovered Gmail connector command catalog
  with read-only test status and mutating-command safety flags.

For Gmail / почта / входящие / письма / ярлыки / черновики / вложения requests,
search `gmail_operations` first. Use `_list_labels` before label-specific work,
`_search_emails` for normal search, and `_read_email_thread` before drafting,
forwarding, tasking, or saving email-derived decisions. Do not send, archive,
delete, label, create/update drafts, or bulk-modify Gmail without explicit owner
approval for the exact action.

## Service Management

- `krasnoyarsk_service_management_playbook.md` - daily workshop control, procurement, repair triage, customer flow, staff, finance, and source intake.
- `service_management_sources.json` - source routing for Krasnoyarsk procurement, personnel, management, and local market context.
- `service_patterns.json` - reusable service-management patterns.
- `phone_flow.json` - phone/mobile workflow expectations.
- `board_cleanup_autopilot_playbook.md` - canonical meaning of `Приберись` and routine board cleanup autonomy.

## Deployment and Operations

- `deployment_runbook.md` - local/server startup, publishing, and private-data boundaries.
- `manager_identity.json` - identity metadata.
- `manager_mcp_catalog.json` - memory/routing MCP commands.
- `crm_mcp_catalog.json` - CRM MCP commands.

## Search Shortcuts

Use these queries before broad browsing:

```powershell
rg -n "BMW|F15|N63|BDC|MEVD|misfire|форсун" docs/agent/bmw_repair_playbook.md docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack
rg -n "BMW|ISTA|AIR|DTC|fault memory|xDrive|ZF|IBS|IHKA|Longlife" docs/agent/bmw_repair_playbook.md docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack
rg -n "ECU|ЭБУ|KOMBI|стрелков|приборк|A2L|ODX|DCM|UDS|J2534|coding|calibration|flash|прошив" docs/agent/ecu_calibration_programming_playbook.md docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack
rg -n "GR Yaris|Yaris GR|GXPA16|G16E|GR-FOUR|EA67F|UC80F" docs/agent/toyota_gr_yaris_playbook.md docs/agent/automotive_sources
rg -n "VIN|OEM|frame|chassis|кузов|каталог" docs/agent
rg -n "DSG|S tronic|DQ200|DQ250|DQ381|DQ500|DL501|DL382|0AM|0CW|02E|0B5|мехатрон|J217|J743|ODIS|SVM|basic settings|адаптац" docs/agent/dsg_transmission_playbook.md docs/agent/automotive_sources/dsg_transmission_sources.json docs/agent/transmission_playbook.md
rg -n "рейк|рулев|запчаст|Красноярск|vendor|seller|scoring|confirmation|source_registry|parts search gateway" docs/agent/ai_parts_krasnoyarsk_playbook.md docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack
rg -n "ИП|Гришкявичус|Гришкевичус|реквизит|карточка предприятия|ОГРНИП|ИНН|ОКВЭД" docs/agent/business_identity_playbook.md data/private_knowledge
rg -n "Gmail|gmail|email|почт|письм|входящие|ярлык|черновик|вложен|_search_emails|_read_attachment" docs/agent
rg -n "fluid|oil|capacity|масло|жидк|заправ" docs/agent
rg -n "Приберись|cleanup|archive|preserve|board" docs/agent
rg -n "source_id|license|ingest|catalog" docs/agent
```

## Update Checklist

When adding or reorganizing knowledge:

1. Classify: domain, source type, license, trust level.
2. Place: update the smallest relevant playbook/catalog, not a duplicate note.
3. Link: add the new route to this index and `knowledge_map.json`.
4. Validate: parse changed JSON and run relevant tests or skill validation.
5. Sync: run `sync_knowledge_base` or `python -m autostop_manager.cli knowledge-sync`.
6. Audit: run `audit_knowledge_base`, `audit_knowledge_annotations`, and
   `audit_skill_registry` when routes, annotations, or skills changed.
7. Journal: record important source intake or rule changes through `manager_journal` when the MCP tool is available.
