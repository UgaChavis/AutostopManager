# AutostopManager

Headless long-term memory and inbox support for the AutoStop CRM manager agent.

This project does not replace AutoStop CRM or Gmail.

AutoStop CRM remains the source of truth for cards, clients, vehicles,
repair orders, and cashbox data.

Gmail remains the source of truth for inbox messages, threads, and email
attachments.

AutostopManager stores only manager memory: facts, notes, lessons, tasks,
reminders, journal entries, operating rules, and durable conclusions extracted
from CRM or email work that should survive between Codex sessions and ChatGPT
mobile usage.

For new files and knowledge expansion, use
`docs/agent/knowledge_base_index.md` first, then
`docs/agent/knowledge_intake_playbook.md`, and keep the tool surface summaries
in `docs/agent/manager_mcp_catalog.json` and `docs/agent/crm_mcp_catalog.json`
up to date.

## Current Manager Layer

- storage: SQLite at `data/autostop_manager.sqlite3`
- local access: `python -m autostop_manager.cli ...`
- MCP access: `python -m autostop_manager.mcp_server`
- docs for agents: `docs/agent/`
- knowledge-base entrypoint: `docs/agent/knowledge_base_index.md`
- knowledge-base machine map: `docs/agent/knowledge_map.json`
- knowledge-base shelf map: `docs/agent/knowledge_shelves.md`
- knowledge-base annotation index: `docs/agent/knowledge_annotations.jsonl`
- Obsidian working vault route:
  `docs/agent/obsidian_knowledge_vault_playbook.md`; prefer the cloud vault
  at `C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM` when available, with
  the desktop mirror at `C:\Users\User\Desktop\Obsidian CRM\AutostopCRM`
- CRM manager data route: `docs/agent/crm_manager_data_playbook.md`; Obsidian
  may hold safe snapshots and quality signals, while raw client databases,
  cashbox ledgers, repair orders, and full board dumps stay in AutoStop CRM
- private business identity route: `docs/agent/business_identity_playbook.md`
  with local private facts under `data/private_knowledge/`
- knowledge-base SQLite sync: `python -m autostop_manager.cli knowledge-sync`
- knowledge-base probe: `python -m autostop_manager.cli knowledge-probe "BMW X5 F15 N63 электрика"`
- knowledge-base search: `python -m autostop_manager.cli knowledge-search "BMW F15 N63 BDC"`
- canonical read-only health audit: `python -m autostop_manager.cli system-audit`
- doctor alias: `python -m autostop_manager.cli doctor`
- knowledge-base audit: `python -m autostop_manager.cli knowledge-audit`
- cleanup dry-run audit: `python -m autostop_manager.cli cleanup-audit`
- read-only CRM health plan from saved payloads:
  `python -m autostop_manager.cli crm-health-plan --board-review-json board_review.json --today-json today_context.json`
- knowledge annotation audit: `python -m autostop_manager.cli annotations-audit`
- memory quality audit: `python -m autostop_manager.cli memory-audit`
- memory curation: `python -m autostop_manager.cli memory-curate --apply`
- task-specific context: `python -m autostop_manager.cli prepare-context "Приберись" --intent board_cleanup`
- compact agent startup package: `python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup`
- skill registry audit: `python -m autostop_manager.cli skills-audit`
- operation ledger: `python -m autostop_manager.cli run-start "Приберись" --intent board_cleanup --dry-run`
- repair source routing: use `docs/agent/automotive_repair_source_playbook.md`
  and `docs/agent/automotive_sources/` before technical repair, TSB, recall,
  diagnostics, labor, fluid, torque, wiring, ADAS, SRS, or HV recommendations
- VIN/OEM lookup routing: use `python -m autostop_manager.cli lookup-oem`
  with `--part-name`, `--side`, `--position`, and optional manual EPC capture
  fields to build a structured original-number dossier before price search
- fluid maintenance routing: use `docs/agent/fluid_maintenance_playbook.md`
  and `docs/agent/automotive_sources/fluid_maintenance_sources.json` before
  oil, operating-fluid, fill-capacity, or ТО service recommendations
- transmission routing: use `docs/agent/transmission_playbook.md`; for VAG DSG,
  S tronic, DQ200/DQ250/DQ381/DQ500/DL501, ODIS, SVM, mechatronic, basic
  settings, or adaptation questions, open
  `docs/agent/dsg_transmission_playbook.md` and
  `docs/agent/automotive_sources/dsg_transmission_sources.json`
- ECU programming routing: use
  `docs/agent/ecu_calibration_programming_playbook.md` and the owner-provided
  `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/`
  for ECU flashing, coding, calibration, UDS/OBD, BMW KOMBI, instrument cluster,
  and "стрелковка" questions; keep odometer, immobilizer, security bypass, and
  emissions-delete requests on official/legal service routes only
- service management routing: use
  `docs/agent/krasnoyarsk_service_management_playbook.md` and
  `docs/agent/service_management_sources.json` before workshop-management
  decisions about parts procurement, repair triage, customer flow, staff load,
  finance control, daily CRM control, or new knowledge intake
- labor work pricing: use `python -m autostop_manager.cli estimate-work` or
  MCP `estimate_repair_work_cost` with public Russia labor-only STO quotes
  plus a public norm-hours/labor-time plausibility layer; it returns average
  work cost, AutoStop `+50%`, norm-hour checks, confidence, and missing context
  without writing repair-order works
- board cleanup autopilot: when the owner says `Приберись`, use
  `docs/agent/board_cleanup_autopilot_playbook.md` as the
  standing procedure for CRM board hygiene with strict preservation of
  user-entered data; fill missing vehicle passport fields from available
  source-backed data; update short readable public descriptions with useful
  paragraphs, emoji markers, and supported CRM Markdown (`**bold**`,
  `*italic*`, `++underline++`) that renders cleanly and no raw technical
  markup, plus the separate plain `board_summary` preview; update
  repair-order data only when the owner explicitly asks to fill or расписывать
  the target ЗН/заказ-наряд; do not move or archive cards without a separate
  explicit owner command
- deployment/runbook: use `docs/agent/deployment_runbook.md`; publish code,
  tests, and playbooks, but keep runtime CRM snapshots and SQLite databases out
  of GitHub
- VIN/OEM lookup: use `docs/agent/vin_oem_lookup_playbook.md` and
  `docs/agent/vin_oem_sources.json` for VIN, chassis, and original catalog
  number dossiers with catalog routes, OEM candidates, supersessions,
  confidence, missing context, and next actions
- email work: use the connected Gmail MCP tools for inbox inspection,
  thread reading, triage, reply drafting, forwarding, and labels
- vehicle identity: use `docs/agent/vehicle_identity_playbook.md` for VIN,
  Japanese chassis numbers, and market-specific vehicle codes before parts
  search
- parts search: use `docs/agent/parts_search_playbook.md` and
  `docs/agent/zzap_search_playbook.md` for Drom, ZZap, Avito sourcing in
  Krasnoyarsk and nearby regions
- AI parts search: use `docs/agent/ai_parts_krasnoyarsk_playbook.md` and
  `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/`
  for Krasnoyarsk vendor discovery, steering-rack/contract-part risk checks,
  offer scoring, seller-call confirmation, pricing logic, and compact reporting
- BMW X5 F15/N63TU: use the indexed BMW route in
  `docs/agent/bmw_repair_playbook.md` and the BMW source cache before F15/N63
  diagnostics, wiring/electronics orientation, BDC/gateway issues, injector
  bulletins, misfires, oil consumption, or cooling analysis
- CRM MCP sync: keep `docs/agent/crm_mcp_catalog.json` aligned with the
  current `autostopcrm-v1` branch in `UgaChavis/AutostopCRM-V1`
- manager MCP sync: keep `docs/agent/manager_mcp_catalog.json` aligned with
  the local AutostopManager MCP surface and memory workflow
- current CRM connector mode: streamable HTTP with optional embedded OAuth
  or bearer-token auth, depending on connector settings

## CLI Examples

```powershell
python -m autostop_manager.cli remember "Аренда бокса оплачивается до 5 числа" --kind fact --tags аренда --confidence 0.9
python -m autostop_manager.cli recall аренда --kind fact --tags аренда
python -m autostop_manager.cli learn "В карточках писать живым языком и коротко" --applies-to crm_cleanup --signal owner_correction --recommendation "Краткая суть: задача, важные факты, деньги/запчасти/проверки; без блоков Статус и Следующий шаг" --avoid "Длинный сухой AI-шаблон" --importance 0.9 --confidence 1.0 --tags карточки,стиль
python -m autostop_manager.cli lessons "живым языком" --applies-to crm_cleanup --tags стиль
python -m autostop_manager.cli memory-context "уборка CRM карточек"
python -m autostop_manager.cli memory-map
python -m autostop_manager.cli memory-topics
python -m autostop_manager.cli memory-gaps
python -m autostop_manager.cli task "Проверить просроченные машины утром" --due 2026-06-01
python -m autostop_manager.cli remind "Напомнить про аренду" --due 2026-06-04T10:00:00+07:00
python -m autostop_manager.cli today
python -m autostop_manager.cli journal "Проверил доску CRM, готовые машины требуют оплаты"
python -m autostop_manager.cli init
python -m autostop_manager.cli seed-rules
python -m autostop_manager.cli lookup-oem WBA00000000000000 --make BMW --part-name "рулевая рейка" --side left --position front
python -m autostop_manager.cli source-route --brand Toyota --data-type repair_manuals
python -m autostop_manager.cli maintenance-fluids --brand Toyota --unit engine_oil --year 2019 --model Camry --engine A25A-FKS --market Russia
python -m autostop_manager.cli service-plan --area parts --city Красноярск --vehicle "Lexus RX200T" --part-number 90311-89014 --urgency today
python -m autostop_manager.cli service-plan --area персонал --role автослесарь --city Красноярск
python -m autostop_manager.cli estimate-work --vehicle "BMW X5" --work "замена рулевой рейки"
python -m autostop_manager.cli estimate-work --vehicle "BMW X5" --work "замена рулевой рейки" --quotes-json quotes.json
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "подобрать сцепление Toyota Yaris GR"
python -m autostop_manager.cli knowledge-probe "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM"
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
python -m autostop_manager.cli knowledge-probe "актуальные реквизиты ИП Гришкявичус"
python -m autostop_manager.cli knowledge-search "8013FE IHKA" --domain bmw_repair
python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "рейка Красноярск vendor discovery offer scoring call confirmation" --domain parts_sourcing
python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
python -m autostop_manager.cli system-audit
python -m autostop_manager.cli doctor
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli crm-health-plan --board-review-json board_review.json --today-json today_context.json
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli memory-audit
python -m autostop_manager.cli memory-curate --apply
```

For `estimate-work`, the plain call is the normal public-research route for
labor-only Russia market prices plus public labor-time checks. Use
`--quotes-json` only for offline/manual quote samples, tests, or no-network
scenarios.

## MCP Tools

The manager memory tools are intentionally separate from CRM operations:

- `remember`
- `recall`
- `learn_from_feedback`
- `recall_lessons`
- `memory_map`
- `memory_topics`
- `memory_context_for`
- `memory_gaps`
- `add_manager_task`
- `today_context`
- `manager_journal`
- `prepare_manager_context`
- `agent_brief`
- `lookup_original_parts`
- `estimate_repair_work_cost`
- `recommend_automotive_sources`
- `recommend_fluid_maintenance_sources`
- `recommend_service_management_actions`
- `sync_knowledge_base`
- `probe_knowledge_base`
- `search_knowledge_base`
- `audit_knowledge_base`
- `audit_knowledge_annotations`
- `audit_skill_registry`
- `system_audit`
- `cleanup_audit`
- `crm_health_plan`
- `audit_memory`
- `curate_memory`
- `start_manager_run`
- `record_manager_run_event`
- `finish_manager_run`
- `list_manager_runs`

## Agent Startup Order

For a new Codex/ChatGPT manager task, start with the compact package:

```powershell
python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
```

Use layers in this order:

1. `agent-brief` for hot rules, command route, allowed/forbidden actions, and
   memory boundaries.
2. Live AutoStop CRM MCP context for CRM work: board, cards, repair orders,
   cashboxes, clients, files, and operational journal.
3. Targeted `knowledge-probe` or `knowledge-search` for local route cards and
   source files.
4. Long playbooks and source packs only after the targeted route is known.

For system maintenance, `system-audit` / `doctor` is the canonical read-only
health layer. It aggregates knowledge, annotations, skills, cleanup dry-run,
local SQLite stats, and manager MCP catalog checks; it reports
`tests_status: external` instead of running pytest. For CRM board hygiene
planning without writes, use `crm-health-plan` on saved `board_review`,
`board_context`, and `today_context` JSON payloads.

Memory is intentionally split:

- local SQLite stores the knowledge index, local rules, and any local CLI
  facts, notes, lessons, tasks, reminders, and journal rows;
- live CRM/MCP memory carries operational tasks, recent work journal, board
  state, and connector context.

An empty local `memory-map` does not mean the manager has no live operational
memory; read MCP context before CRM decisions.

When the owner provides new files, treat them as source material, not memory.
Extract durable rules, update the relevant playbook or catalog, then store
only the reusable conclusion in memory.

Before broad local file reads, use `prepare_manager_context` for non-trivial
tasks, then the indexed knowledge-base tools: `probe_knowledge_base` first,
`search_knowledge_base` inside the returned domain when more detail is needed,
`sync_knowledge_base` after source/catalog/annotation changes,
`audit_knowledge_base` and `audit_knowledge_annotations` after intake. The goal
is to find the right command route, compact annotation, playbook, source
catalog, or model-specific skill first, then read only that narrow file.

For long-term memory quality, use `memory-audit` before cleanup and
`memory-curate --apply` only for non-destructive duplicate archiving. Memory
records may carry `importance`, `confidence`, `expires_at`, `supersedes_id`,
`sensitivity`, `last_used_at`, and `archived_at`; these fields guide recall
ranking and help keep obsolete memories out of normal context.

For autopilot, procurement, finance, knowledge intake, and other multi-step CRM
work, use the manager run ledger: start a run, record planned actions/skips/
risks/writes, finish it with verification evidence, and journal only the
durable summary.

AutoStop CRM operations still use the existing AutoStop CRM MCP tools such as
`bootstrap_context`, `get_board_context`, `review_board`, `search_cards`,
`get_card_context`, and `list_repair_orders`.

Gmail work uses the connected Gmail MCP tools for mailbox inspection and
message handling. Keep raw email content out of manager memory unless it
represents a durable fact, decision, task, or reminder.

Parts sourcing uses the dedicated playbook in
`docs/agent/ai_parts_krasnoyarsk_playbook.md`, the general playbook in
`docs/agent/parts_search_playbook.md`, and the ZZap-specific playbook in
`docs/agent/zzap_search_playbook.md`. Keep only the chosen part, seller,
confirmation status, and reusable search heuristic in memory.

Transmission questions use `docs/agent/transmission_playbook.md`; VAG DSG and
Audi S tronic questions use `docs/agent/dsg_transmission_playbook.md` plus
`docs/agent/automotive_sources/dsg_transmission_sources.json` before software,
adaptation, mechatronic, fluid, or used-unit guidance.

ECU programming, calibration, coding, BMW KOMBI, instrument-cluster, and
"стрелковка" questions use
`docs/agent/ecu_calibration_programming_playbook.md` and the local ECU
knowledge pack. Keep unsafe requests such as odometer tampering, immobilizer or
security bypass, EEPROM/NVM cloning, and emissions deletes out of procedural
answers; route them to official/legal service procedures only.

Vehicle identity uses the dedicated playbook in
`docs/agent/vehicle_identity_playbook.md`. Keep the stable routing rule in
memory: classify the identifier first, then choose the market-appropriate
decode path, then hand off to parts sourcing if needed.

Private ИП / AutoStop business identity uses
`docs/agent/business_identity_playbook.md` plus local files under
`data/private_knowledge/`. Use that route for current requisites, company-card
data, ИП Гришкявичус/Гришкевичус, and old-versus-current business document
sorting. Keep private bank/contact details out of Git-tracked docs and verify
exact wording from the source document before external use.

The owner's canonical command `Приберись` means board-cleanup
autopilot. Use `docs/agent/board_cleanup_autopilot_playbook.md`: read the live
CRM board, classify blockers, fill vehicle passport fields from available
source-backed data, enrich VIN/OEM/parts/service data, update short card notes,
rewrite short readable public descriptions with emoji markers and supported
CRM Markdown (`**bold**`, `*italic*`, `++underline++`), update
tags/indicators/deadlines, recommend archive candidates when safe, leave cards
in their current columns and archive state unless separately commanded, update
repair orders only by explicit owner request, and preserve user-entered works,
materials, prices, payments, files, contacts, and diagnostics.

## CRM MCP Sync

The canonical MCP surface for AutoStop CRM lives in the CRM repository:

- repository: `UgaChavis/AutostopCRM-V1`
- active development branch: `autostopcrm-v1`
- connector docs: `README.md`, `docs/OPERATIONS_RUNBOOK.md`, `MCP_GUIDE.md`,
  `API_GUIDE.md`, `CHATGPT_CONNECTOR_SETUP.md`
- runtime files: `src/minimal_kanban/mcp/server.py`, `src/minimal_kanban/mcp/main.py`,
  `src/minimal_kanban/mcp/runtime.py`, `src/minimal_kanban/mcp/auth.py`

When the CRM repository adds or renames MCP tools, update
`docs/agent/crm_mcp_catalog.json` and the startup/playbook docs in this repo.

## Deployment

Local MCP startup:

```powershell
python -m autostop_manager.mcp_server
```

Server deployment requires an explicit host/platform and credential path. Keep
`data/` local: it may contain CRM snapshots, client contacts, VINs, payments,
or other runtime evidence that must not be pushed to GitHub.
