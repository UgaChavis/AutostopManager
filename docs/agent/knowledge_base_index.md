# Knowledge Base Index

Purpose: one entry point for AutostopManager knowledge. Start here when the owner asks to expand, search, audit, or use the local knowledge base.

## Fast Route

1. Run `agent_brief` for a new agent task, then `probe_knowledge_base` or
   `python -m autostop_manager.cli knowledge-probe "<query>"`.
2. If `has_knowledge=true`, open `open_first` / `source_of_truth` before broad file reads. Use returned `reference_files` only when the task specifically asks for a linked pack artifact, schema, manifest, source catalog, or implementation draft.
3. If more detail is needed, run `search_knowledge_base` inside the returned `best_domain`.
4. If `has_knowledge=false`, route to external/OEM sources and consider knowledge intake after the answer.
5. If the task is technical automotive work, collect VIN/chassis, market, engine, transmission, mileage, complaint, and scan results before giving facts.
6. If adding new knowledge, follow `knowledge_intake_playbook.md`, update this index plus `knowledge_map.json`, then run `sync_knowledge_base`.
7. Keep raw evidence out of memory and Git unless explicitly safe. Store durable rules, source routes, and short verified conclusions.
8. For client/cashbox/repair-order manager summaries, use
   `crm_manager_data_playbook.md`: return safe summaries and quality signals,
   while raw client data, cash journals, and repair orders stay in CRM.
10. For system health, run `system-audit` or `doctor`; it aggregates local
    audits and reports `tests_status: external` without running pytest.
11. For cleanup candidates, run `cleanup-audit`; it is dry-run only and never
    deletes files or writes to CRM.
12. For CRM board health planning, use `crm-health-plan` with saved
    `board_review`, `board_context`, and `today_context` JSON payloads; it is
    read-only and reports zero card moves/archives.

## Indexed Navigation

Use these commands when working locally:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "BMW X5 кузов E15 мотор N63 электрика"
python -m autostop_manager.cli knowledge-probe "подобрать сцепление Toyota Yaris GR"
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-probe "в карточке CRM VIN найти OEM свечей аналоги закупка записать"
python -m autostop_manager.cli knowledge-probe "MAN VIN webMANTIS partslink24 фильтра MAHLE Bosch MANN"
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
python -m autostop_manager.cli system-audit
python -m autostop_manager.cli doctor
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli crm-health-plan --board-review-json board_review.json --today-json today_context.json
```

Use these MCP tools when working through the manager:

- `agent_brief` - mandatory compact startup package before broad document
  reads or CRM work: route, memory_sources, hot rules, allowed/forbidden
  actions, read order, and verification.
- `prepare_manager_context` - combine command routes, relevant memory/rules,
  knowledge routing, missing required context, and next actions.
- `sync_knowledge_base` - refresh the SQLite index after docs/catalog/skill changes.
- `probe_knowledge_base` - cheaply decide whether local knowledge exists and
  which source-of-truth file to open first; for routes with
  `optional_runtime_files`, it also reports available/missing runtime files and
  whether exact private facts are locally available.
- `search_knowledge_base` - find the right route before reading full files.
- `audit_knowledge_base` - verify route cards, mapped files, index counts, and
  FTS health after source intake.
- `audit_knowledge_annotations` - verify compact sidecar annotations used for
  fast file-level routing before broad section reads.
- `audit_skill_registry` - verify linked local Codex skills exist and are
  mapped to knowledge domains.
- `system_audit` - canonical read-only health layer: knowledge audit,
  annotations audit, skills audit, cleanup dry-run summary, local SQLite stats,
  manager MCP catalog consistency, and external test status.
- `cleanup_audit` - dry-run cleanup candidate report; it recommends actions
  but never deletes, moves, archives, or edits files/CRM.
- `crm_health_plan` - read-only CRM health plan from already fetched
  `board_context`, `board_review`, and `today_context` payloads; it never calls
  live write tools and reports `cards_moved=0` and `cards_archived=0`.
- `decode_vehicle_identity` - source-aware VIN/frame/body-number identity
  dossier: classification, check digit/model-year diagnostics, vPIC/WMI/platform
  evidence, CRM conflicts, confidence, adapter status, and required EPC/API
  sources before VIN-critical parts lookup.
- `decode_vehicle_identities` - batch version for CRM board scans and
  multi-card VIN/frame quality reports; uses public vPIC batch when live vPIC is
  enabled, reports batch coverage, and keeps raw customer identifiers out of
  durable memory and fixtures.
- `catalog_provider_status` - read-only readiness report for VIN/OEM/cross and
  procurement providers; never prints secret values, only configured/missing
  env names.
- `plan_oem_parts_providers` - provider/blocker plan for VIN/frame -> OEM ->
  crosses/applicability -> procurement/RF market price; redacts identifiers and
  does not call suppliers or write CRM.
- `vin17_decode_vehicle` - read-only 17VIN adapter/dry-run for vehicle decode
  after `VIN17_ACCOUNT` and `VIN17_SECRET` are configured; never exposes token
  or secret values.
- `vin17_search_part_number_by_vin` - read-only 17VIN part-number-by-VIN
  adapter/dry-run after a 17VIN decode has returned an EPC code.
- `partsapi_catalog_lookup` - read-only PartsAPI adapter/dry-run for
  `VINdecode`, `VINdecodeOE`, `getPartsbyVIN`, `getOEApplicability`, `getCrosses`,
  `getCrossesWithBrand`, `getCrossesTitle`, `getArticleCrosses`,
  `searchArticles`, `getEngine`, `getSearchTree`, `getArticles`, `getArticle`,
  and `getArticleCriteria`; live calls require
  `PARTSAPI_BASE_URL` plus either `PARTSAPI_KEY` or a method-specific
  `PARTSAPI_*_KEY`.
- `resolve_vin_oem_parts` - read-only PartsAPI-first resolver for one
  VIN/frame/body-number and requested part: identity, category index routing,
  OEM candidates, enrichment, readiness gates, and manual CRM confirmation gate.
- `search_partsapi_category_index` / `explain_partsapi_category_for_intent` /
  `validate_partsapi_category_index` - inspect the local numeric PartsAPI
  category index used before live `getPartsbyVIN`.
- `benchmark_vin_parts_lookup` - read-only batch benchmark for 10-card or CRM
  VIN/frame/body-number quality checks: identity confidence, part-intent
  recognition, safe public query coverage, PartsAPI/17VIN dry-run readiness,
  opt-in PartsAPI OE identity cross-check, and missing live catalog/supplier
  env names with raw identifiers redacted.
- `build_vin_parts_work_order` - read-only per-card work order after benchmark:
  OEM/EPC routes, prepared API checks, cross/applicability steps, supplier
  sequence, read-only candidate lookup vs CRM writeback gates, and acceptance
  checklists with raw identifiers redacted.
- `plan_crm_vin_oem_parts_lookup` - deterministic workflow planner for CRM
  card VIN/frame/body-number parts lookup: card intake, vehicle identity, OEM,
  supersession/cross, закупка/RF market quote matrix, selected-part CRM material
  rows, and reread verification.

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
- `optional_runtime_files` - local/private ignored files that should be indexed
  when present, but may be absent in a clean checkout.

The agent should use route cards as the cheap first pass. Full document search
is the second pass. `reference_files` should remain visible in probe output but
should not produce full section matches in normal search.

## Core Control Files

- `autostop_manager_skill.md` - agent startup routine, role, memory boundaries, and canonical behavior.
- `manager_rules.json` - durable operating rules with priorities.
- `knowledge_shelves.md` - shelf map, file placement rules, route-card contract, and maintenance checklist.
- `crm_manager_data_playbook.md` - safety boundary and refresh workflow for
  manager-facing CRM statistics, client-quality signals, cashbox overviews, and
  repair-order summaries.
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

## 3D Printing CAD

- `3d_printing_cad_playbook.md` - route for photo/description to CAD model,
  STL validation, Anycubic Kobra S1 print preparation, slicer handoff, and the
  boundary between source geometry, generated STL, and generated G-code.
- `C:/Users/User/Desktop/3д/` - local CAD/STL workspace with OpenSCAD/BOSL2
  models, printer profile, calibration measurements, export reports, and
  helper scripts.

For 3D printing / Anycubic / STL / OpenSCAD / bolts / clips / clamps requests,
search `3d_printing_cad` first. Use the local CAD project rules, then build and
validate the target model before opening it in Anycubic Slicer Next. Do not
install custom firmware, generate G-code, or start a print without explicit
owner intent for that step.

## Knowledge Intake

- `knowledge_intake_playbook.md` - required workflow for new files, links, PDFs, spreadsheets, scans, catalogs, and owner notes.
- `knowledge_shelves.md` - where durable knowledge belongs, how route cards are marked, and how source packs should be signed.
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
- `automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/` - compact owner-provided ECU calibration/programming reference pack with Markdown, data, and source indexes.
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
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/MANIFEST.md`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/sources/`

For ECU programming, calibration formats, UDS/OBD/J2534, flash recovery, BMW ISTA/I-level/VO/FA, or "стрелковка" / KOMBI / instrument-cluster questions, search `ecu_calibration_programming` first. Treat cluster/needle requests as legal coding/adaptation diagnostics only; if the request touches odometer, VIN, EEPROM/NVM dump cloning, immobilizer, security bypass, or emissions delete, route to official/legal service procedure and do not provide bypass steps.

General BMW repair pack:

- `docs/agent/bmw_repair_playbook.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/README_ru.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/manifest.json`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/data/`

For generic BMW diagnostics, DTC/fault memory, xDrive, ZF transmission,
electronics, HV, or fluids questions, search `bmw_repair` first; then narrow to
the BMW F15/N63 route when the vehicle matches.

BMW X5 F15/N63TU route:

- `docs/agent/bmw_repair_playbook.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/README_ru.md`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/manifest.json`
- `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/data/`

For BMW F15/N63 requests, load the BMW route first, then route safety-critical or VIN-specific facts through ISTA/AIR/ETK or BMW official sources.

Toyota GR Yaris route:

- `docs/agent/toyota_gr_yaris_playbook.md`

For Toyota GR Yaris / Yaris GR / GXPA16 / G16E-GTS requests, load the
playbook first, then verify VIN/frame-specific repair, TSB, wiring, torque,
fluid, recall, and OEM part facts through Toyota official or licensed sources.

Fluid maintenance has a dedicated Codex skill:

- `C:/Users/User/.codex/skills/autostop-fluid-maintenance/SKILL.md`
- `docs/agent/fluid_maintenance_playbook.md`
- `docs/agent/automotive_sources/fluid_maintenance_sources.json`

For oil, fluid, approval, and fill-capacity requests, load the skill first,
then verify exact vehicle/unit data through OEM or licensed service sources.

## Vehicle Identity and OEM Parts

- `crm_vin_oem_parts_lookup_playbook.md` - end-to-end CRM card VIN/frame/body-number -> OEM -> replacements/crosses -> закупка/RF market prices -> structured CRM writeback workflow.
- `vehicle_identity_playbook.md` - classify VIN, Japanese frame/chassis number, Korean VIN, and market-specific codes.
- `vin_oem_lookup_playbook.md` - original catalog number lookup routing.
- `partsapi_method_contracts.md` - PartsAPI method inputs, normalized output buckets, and smoke-test notes for VIN/OEM lookup.
- `vin_oem_sources.json` - VIN/OEM source catalog.
- `parts_search_playbook.md` - Drom/marketplace sourcing workflow.
- `zzap_search_playbook.md` - ZZap price comparison, replacements, and local-region checks.
- `procurement_pricing_playbook.md` - закупочная цена, Красноярск-first availability, selected-part vs OEM-reference separation, package/unit math, and CRM material-total rules.
- `procurement_price_sources.json` - supplier/API catalog for ROSSKO/Роска, Armtek, Autopiter, AutoEuro, ZZap, AutoSputnik, APEC, PartsAPI, UMAPI, AUTOPOISK, Mikado, local Krasnoyarsk sources, access modes, and quote fields.
- `ai_parts_krasnoyarsk_playbook.md` - AI parts search, local Красноярск vendor discovery, offer scoring, seller-call confirmation, and high-risk parts such as рулевая рейка.
- `automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/` - compact owner-provided AI Parts Search Krasnoyarsk/Russia project pack with retained workflow docs only.
- `automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/` - official public offline PDF/XLSX catalog source pack and usage rules.
- `data/offline_parts_catalogs/catalog_index.json` - optional local runtime index for downloaded PDF/XLSX catalogs and extracted text.

AI parts Красноярск project pack:

- `C:/Users/User/.codex/skills/autostop-parts-pricing/SKILL.md`
- `docs/agent/ai_parts_krasnoyarsk_playbook.md`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/README.md`
- `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/MANIFEST.md`

MAN/OE and official aftermarket catalog route:

- `docs/agent/vin_oem_lookup_playbook.md`
- `docs/agent/crm_vin_oem_parts_lookup_playbook.md`
- `docs/agent/vin_oem_sources.json`
- `docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/README.md`

For MAN trucks, buses, vans, or engines, route OEM number lookup through MAN
Service Portal/webMANTIS, MAN partslink24, Parts-Catalogs, PartsAPI, 17VIN, or
dealer/supplier confirmation. Do not use third-party MANTIS/EPC downloads as
official evidence. For filters, plugs, sensors, and other расходники, use
official manufacturer catalogs such as MAHLE, Bosch, MANN-FILTER, Hengst,
Donaldson, Fleetguard, NGK/NTK, and TecDoc as cross/applicability evidence
after the genuine reference or exact vehicle profile is known. When the local
offline cache is present, check it first with:
`rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text`.
The cache is not a MAN offline EPC; it is an official aftermarket/supporting
source layer for fitment, dimensions, notes, and cross checks.

For spare-parts search, steering rack / рулевая рейка, used/contract parts,
seller discovery, call confirmation, offer scoring, supplier routing, or local
Красноярск availability questions, search `parts_sourcing` first. Do not treat
marketplace listings or supplier-site stock text as confirmed availability
without API/cabinet/phone/message confirmation.

## Business Identity

- `business_identity_playbook.md` - private local route for current ИП
  requisites, company-card data, AutoStop commercial-offer identity, and
  document freshness decisions.
- `data/private_knowledge/business_identity_current.json` - optional private current
  facts selected from the newest reliable documents. This file is local runtime
  knowledge and must not be committed.
- `data/private_knowledge/business_documents_inventory.json` - optional private
  filesystem inventory of `C:/Users/User/Мой диск/ДОКУМЕНТЫ`, with dates,
  hashes, and topic flags.

For ИП / реквизиты / карточка предприятия / ИП Гришкявичус or Гришкевичус
requests, search `business_identity` first. If the optional private JSON files
are absent, use the playbook/annotation only for routing and say that exact
current реквизиты are unavailable until local runtime files are restored. Before
external use, verify exact banking/legal wording against the original source
document if formatting matters.

## Business Documents

- `business_document_quality_playbook.md` - AutoStop route for PDF/DOCX/XLSX
  invoices, acts, КП, receipts, requisites sheets, accounting-style documents,
  and printable forms.
- `C:/Users/User/.codex/skills/autostop-business-documents/SKILL.md` - local
  Codex skill with the mandatory document-quality gate.

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
- `crm_manager_data_playbook.md` - what operational CRM data can be summarized
  and what must remain live-only in CRM.
- `service_management_sources.json` - source routing for Krasnoyarsk procurement, personnel, management, and local market context.
- `service_patterns.json` - reusable service-management patterns.
- `phone_flow.json` - phone/mobile workflow expectations.
- `board_cleanup_autopilot_playbook.md` - canonical meaning of `Приберись` and routine board cleanup autonomy.

## Work Labor Pricing

- `work_labor_pricing_playbook.md` - read-only labor estimate workflow,
  public Russia STO sample rules, AutoStop `+50%` pricing formula, and
  norm-hours plausibility layer.
- `labor_pricing_sources.json` - source catalog for public labor pricing and
  labor-time cross-checks.

Use `estimate_repair_work_cost` only for estimates. It must not write
repair-order works or materials without a separate explicit owner command.

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
rg -n "рейк|рулев|запчаст|Красноярск|vendor|seller|scoring|confirmation|pricing|reporting" docs/agent/ai_parts_krasnoyarsk_playbook.md docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack
rg -n "ИП|Гришкявичус|Гришкевичус|реквизит|карточка предприятия|ОГРНИП|ИНН|ОКВЭД" docs/agent/business_identity_playbook.md
# If data/private_knowledge exists locally, search it too; it is optional and ignored by Git.
rg -n "Gmail|gmail|email|почт|письм|входящие|ярлык|черновик|вложен|_search_emails|_read_attachment" docs/agent
rg -n "fluid|oil|capacity|масло|жидк|заправ" docs/agent
rg -n "Приберись|прибейсь|переберись|cleanup|archive|preserve|board|описание|emoji|эмодзи" docs/agent
rg -n "source_id|license|ingest|catalog" docs/agent
rg -n "3D|3д|Anycubic|Kobra|STL|OpenSCAD|BOSL2|PLA|PETG|clip|clamp|thread|болт|гайк|клипс|хомут" docs/agent/3d_printing_cad_playbook.md C:/Users/User/Desktop/3д/docs C:/Users/User/Desktop/3д/AGENTS.md
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
