# AutostopManager

Headless long-term memory and inbox support for the AutoStop CRM manager agent.

This project does not replace AutoStop CRM or Gmail.

AutoStop CRM remains the source of truth for cards, clients, vehicles,
repair orders, and cashbox data.

Gmail remains the source of truth for inbox messages, threads, and email
attachments.

AutostopManager stores only manager memory: facts, notes, tasks, reminders,
journal entries, operating rules, and durable conclusions extracted from CRM
or email work that should survive between Codex sessions and ChatGPT mobile
usage.

For new files and knowledge expansion, use
`docs/agent/knowledge_base_index.md` first, then
`docs/agent/knowledge_intake_playbook.md`, and keep the tool surface summaries
in `docs/agent/manager_mcp_catalog.json` and `docs/agent/crm_mcp_catalog.json`
up to date.

## First Version

- storage: SQLite at `data/autostop_manager.sqlite3`
- local access: `python -m autostop_manager.cli ...`
- MCP access: `python -m autostop_manager.mcp_server`
- docs for agents: `docs/agent/`
- knowledge-base entrypoint: `docs/agent/knowledge_base_index.md`
- knowledge-base machine map: `docs/agent/knowledge_map.json`
- knowledge-base shelf map: `docs/agent/knowledge_shelves.md`
- knowledge-base SQLite sync: `python -m autostop_manager.cli knowledge-sync`
- knowledge-base probe: `python -m autostop_manager.cli knowledge-probe "BMW X5 F15 N63 электрика"`
- knowledge-base search: `python -m autostop_manager.cli knowledge-search "BMW F15 N63 BDC"`
- knowledge-base audit: `python -m autostop_manager.cli knowledge-audit`
- repair source routing: use `docs/agent/automotive_repair_source_playbook.md`
  and `docs/agent/automotive_sources/` before technical repair, TSB, recall,
  diagnostics, labor, fluid, torque, wiring, ADAS, SRS, or HV recommendations
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
- board cleanup autopilot: when the owner says `Приберись`, use
  `docs/agent/board_cleanup_autopilot_playbook.md` as the standing procedure
  for a full CRM board cleanup with strict preservation of user-entered data
- deployment/runbook: use `docs/agent/deployment_runbook.md`; publish code,
  tests, and playbooks, but keep runtime CRM snapshots and SQLite databases out
  of GitHub
- VIN/OEM lookup: use `docs/agent/vin_oem_lookup_playbook.md` and
  `docs/agent/vin_oem_sources.json` for VIN, chassis, and original catalog
  number routing
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
  offer scoring, seller-call confirmation, schemas, configs, and OpenAPI drafts
- BMW X5 F15/N63TU: use the dedicated local skill at
  `C:/Users/User/.codex/skills/bmw-f15-n63/SKILL.md` before F15/N63
  diagnostics, wiring/electronics orientation, BDC/gateway issues, injector
  bulletins, misfires, oil consumption, or cooling analysis
- CRM MCP sync: keep `docs/agent/crm_mcp_catalog.json` aligned with the
  current `autostopCRM` branch in `UgaChavis/AutostopCRM-V1`
- manager MCP sync: keep `docs/agent/manager_mcp_catalog.json` aligned with
  the local AutostopManager MCP surface and memory workflow
- current CRM connector mode: streamable HTTP with optional embedded OAuth
  or bearer-token auth, depending on connector settings

## CLI Examples

```powershell
python -m autostop_manager.cli remember "Аренда бокса оплачивается до 5 числа" --kind fact --tags аренда
python -m autostop_manager.cli recall аренда
python -m autostop_manager.cli task "Проверить просроченные машины утром" --due 2026-04-30
python -m autostop_manager.cli remind "Напомнить про аренду" --due 2026-05-04T10:00:00+07:00
python -m autostop_manager.cli today
python -m autostop_manager.cli journal "Проверил доску CRM, готовые машины требуют оплаты"
python -m autostop_manager.cli init
python -m autostop_manager.cli seed-rules
python -m autostop_manager.cli lookup-oem 1HGCM82633A004352 --model-year 2003
python -m autostop_manager.cli source-route --brand Toyota --data-type repair_manuals
python -m autostop_manager.cli maintenance-fluids --brand Toyota --unit engine_oil --year 2019 --model Camry --engine A25A-FKS --market Russia
python -m autostop_manager.cli service-plan --area parts --city Красноярск --vehicle "Lexus RX200T" --part-number 90311-89014 --urgency today
python -m autostop_manager.cli service-plan --area персонал --role автослесарь --city Красноярск
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-probe "подобрать сцепление Toyota Yaris GR"
python -m autostop_manager.cli knowledge-probe "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM"
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
python -m autostop_manager.cli knowledge-search "8013FE IHKA" --domain bmw_repair
python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "рейка Красноярск vendor discovery offer scoring call confirmation" --domain parts_sourcing
python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
python -m autostop_manager.cli knowledge-audit
```

## MCP Tools

The manager memory tools are intentionally separate from CRM operations:

- `remember`
- `recall`
- `add_manager_task`
- `today_context`
- `manager_journal`
- `lookup_original_parts`
- `recommend_automotive_sources`
- `recommend_fluid_maintenance_sources`
- `recommend_service_management_actions`
- `sync_knowledge_base`
- `probe_knowledge_base`
- `search_knowledge_base`
- `audit_knowledge_base`

When the owner provides new files, treat them as source material, not memory.
Extract durable rules, update the relevant playbook or catalog, then store
only the reusable conclusion in memory.

Before broad local file reads, use the indexed knowledge-base tools:
`probe_knowledge_base` first, `search_knowledge_base` inside the returned domain
when more detail is needed, `sync_knowledge_base` after source/catalog changes,
and `audit_knowledge_base` after intake. The goal is to find the right
playbook, source catalog, or model-specific skill first, then read only that
narrow file.

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

The owner's natural command `Приберись` means board-cleanup autopilot. Use
`docs/agent/board_cleanup_autopilot_playbook.md`: read the live CRM board,
classify blockers, enrich VIN/OEM/parts/service data, update short card notes,
move/tag/mark/archive when safe, and preserve user-entered works, materials,
prices, payments, files, contacts, and diagnostics.

## CRM MCP Sync

The canonical MCP surface for AutoStop CRM lives in the CRM repository:

- repository: `UgaChavis/AutostopCRM-V1`
- active development branch: `autostopCRM`
- connector docs: `MCP_GUIDE.md`, `CHATGPT_CONNECTOR_SETUP.md`,
  `GPT_AGENT_09_MCP_COMMAND_CATALOG.md`, `GPT_AGENT_10_MCP_OPERATION_FLOWS.md`
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
