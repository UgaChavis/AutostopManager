# AutostopManager

Headless long-term memory, routing, and document hygiene layer for the AutoStop
manager agent. It does not replace AutoStop CRM or Gmail.

- AutoStop CRM remains the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and board state.
- Gmail remains the source of truth for mail, threads, labels, drafts,
  attachments, and sent history.
- AutostopManager stores only durable manager memory, routing rules, playbooks,
  compact catalogs, and local knowledge indexes.

## Canonical Docs

Start with these files instead of broad-reading the whole tree:

- `docs/agent/knowledge_base_index.md` - compact human navigation.
- `docs/agent/knowledge_shelves.md` - placement, deletion, and source-pack
  policy.
- `docs/agent/knowledge_map.json` - machine route cards.
- `docs/agent/knowledge_annotations.jsonl` - compact file annotations.
- `docs/agent/command_routes.json` - natural owner-command routes.
- `docs/agent/autostop_manager_skill.md` - manager startup behavior.
- `docs/agent/manager_mcp_catalog.json` - local manager MCP surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM MCP surface.
- `docs/agent/gmail_workflow_playbook.md` and
  `docs/agent/gmail_mcp_catalog.json` - Gmail operations.
- `docs/agent/business_document_quality_playbook.md` - PDF/DOCX/XLSX quality
  gate.
- `docs/agent/board_cleanup_autopilot_playbook.md` - canonical `Приберись`
  behavior.

## Daily Entry Points

```powershell
python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
python -m autostop_manager.cli prepare-context "почисти документацию" --intent documentation_hygiene
python -m autostop_manager.cli knowledge-probe "проверить Gmail коннектор почта ярлыки вложения"
python -m autostop_manager.cli knowledge-search "счет НДС PDF render" --domain business_documents
```

For a new Codex/ChatGPT manager task, use `agent-brief` first, then live CRM
MCP context for CRM work, then `knowledge-probe` / `knowledge-search` for local
docs. Long source packs are cold references and should be opened only after the
route card or playbook says they are relevant.

## Core Commands

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli system-audit
python -m autostop_manager.cli doctor
python -m autostop_manager.cli control-report --format json --output frontend/control-center/control-report.json
python -m autostop_manager.cli environment-report --format markdown --output reports/environment-report.md
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli memory-audit
python -m autostop_manager.cli memory-review
python -m autostop_manager.cli memory-curate --apply
python -m autostop_manager.cli knowledge-intake --path docs/agent/knowledge_map.json --dry-run
python -m autostop_manager.cli prepare-card-action --card-id card_123 --expected-updated-at 2026-06-08T10:00:00+07:00 --description "Проверить течь" --target-fields description
python -m autostop_manager.cli provider-smoke --provider all --mode dry-run
```

`system-audit` is read-only and aggregates knowledge, annotation, skill,
cleanup, SQLite, and manager MCP catalog checks. It reports test status as
external; run pytest separately when changing docs/contracts.

On Windows/local Codex hosts, use `python -m autostop_manager.cli doctor` and
the audit commands above. `scripts/doctor.sh` is the server/Unix deep-check
route for `/opt/AutostopManager` plus the adjacent CRM checkout; it needs bash,
Linux paths, Docker, nginx, and the production service context. Use
`bash scripts/doctor.sh --full` only on a host that has those prerequisites.

`control-report` writes the local Control Center payload. `environment-report`
is a CLI alias over the same safe data with emphasis on server/Codex/runtime
readiness: OS, disk, memory, public ports, core tools, skills/plugins, git
hooks, venv packages, env key presence without values, provider readiness, and
production ops gates. Open `frontend/control-center/index.html` after
generating `frontend/control-center/control-report.json`.

## Manager MCP Tools

The manager MCP tools are intentionally separate from CRM operations. Keep
`docs/agent/manager_mcp_catalog.json` aligned with
`autostop_manager.mcp_tools.register_manager_memory_tools`.

Current families:

- startup/context: `today_context`, `prepare_manager_context`,
  `agent_brief`, `prepare_crm_card_action`;
- memory: `remember`, `recall`, `learn_from_feedback`, `memory_context_for`,
  `memory_map`, `memory_topics`, `memory_gaps`;
- knowledge: `sync_knowledge_base`, `probe_knowledge_base`,
  `search_knowledge_base`, `audit_knowledge_base`,
  `audit_knowledge_annotations`, `audit_skill_registry`;
- health: `system_audit`, `cleanup_audit`, `crm_health_plan`;
- control center: `control_report`, `provider_smoke_report`;
- operations: `start_manager_run`, `record_manager_run_event`,
  `finish_manager_run`, `list_manager_runs`;
- memory quality: `audit_memory`, `curate_memory`, `memory_review`,
  `memory_review_apply`;
- knowledge intake: `knowledge_intake_plan`;
- automotive helpers: `lookup_original_parts`, `decode_vehicle_identity`,
  `catalog_provider_status`, `lookup_oem_catalog_candidates`,
  `plan_crm_vin_oem_parts_lookup`, `benchmark_vin_parts_lookup`,
  `build_vin_parts_work_order`, `estimate_repair_work_cost`,
  `recommend_automotive_sources`, `recommend_fluid_maintenance_sources`,
  `recommend_service_management_actions`.

For VIN/OEM work, use `docs/agent/vehicle_identity_playbook.md`,
`docs/agent/vin_oem_lookup_playbook.md`, and
`docs/agent/crm_vin_oem_parts_lookup_playbook.md`. Live EPC calls require
configured provider credentials; dry-run output must redact customer
identifiers and secrets.

## CRM Rules

Use AutoStop CRM MCP for live cards, clients, vehicles, repair orders,
cashboxes, files, and board state. Start broad CRM work with
`bootstrap_context`, `get_board_context`, or `review_board`.

For `Приберись`, open `docs/agent/board_cleanup_autopilot_playbook.md`.
The standing rule is:

- fill missing vehicle passport fields from available source-backed data;
- rewrite public descriptions as short readable notes with paragraphs, useful
  emoji markers, and CRM-supported Markdown (`**bold**`, `*italic*`,
  `++underline++`);
- keep `board_summary` plain and short;
- do not move or archive cards without a separate explicit owner command;
- write repair-order rows only when the owner explicitly asks to fill or
  расписать the target ЗН/заказ-наряд.

CRM deadline tools may expose slightly different schemas through different MCP
surfaces. Before write work, inspect the current tool schema; the live CRM
deadline payload supports duration fields such as days/hours/minutes/seconds
or total seconds even when a static wrapper describes the field loosely.

## Gmail Rules

Use `docs/agent/gmail_workflow_playbook.md` before Gmail work. Read/search
tools are safe when needed for the task. Archive, delete, label, draft, send,
and forward actions require explicit owner approval for the exact target.

For bulk mailbox cleanup, preview the Gmail query with search first, then use
server-side bulk actions only after the target set is clear. Never store raw
private threads, full bodies, or attachment contents in manager memory.

## Business Documents

For invoices, acts, КП, receipts, requisites sheets, and PDF/DOCX/XLSX files,
use `docs/agent/business_document_quality_playbook.md`.

Before saying a document is ready, verify source facts, реквизиты, number/date,
totals, НДС wording/status, signatures/stamps, page breaks, and render every
meaningful page or sheet. Generated PDFs such as `generated_invoices/` are local
artifacts and are ignored by Git unless explicitly promoted.

## Knowledge Hygiene

Documentation should stay compact:

- keep procedures in playbooks, route metadata in `knowledge_map.json`, and
  search summaries in `knowledge_annotations.jsonl`;
- delete obsolete files after migrating any unique active rule;
- keep large automotive source packs as compact cold references with README,
  MANIFEST, source/license notes, and important CSV/JSON/JSONL tables;
- do not commit raw CRM exports, Gmail bodies, private business requisites,
  SQLite databases, OAuth state, supplier credentials, or local generated PDFs.

When reorganizing docs, run the audit commands above and the focused pytest set
covering knowledge routing, MCP catalogs, cleanup audit, and CLI contracts.
