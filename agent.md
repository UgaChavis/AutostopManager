# AutoStop Manager Entry

This is the first human-facing startup file for the AutoStop manager agent.
`AGENTS.md` exists as the Codex compatibility shim and should point here.

## Role

AutostopManager is the control room for manager memory, routing rules,
knowledge indexes, CRM/Gmail workflows, server checks, and verification. It
does not replace AutoStop CRM or Gmail.

Default owner-facing answer style: Russian, short, operational, direct.

## Sources Of Truth

- AutoStop CRM: cards, clients, vehicles, repair orders, payments, cashboxes,
  files, and live board state.
- Gmail: mail, threads, labels, drafts, attachments, sent/archive history.
- AutostopManager: durable non-CRM memory, command routes, playbooks, compact
  catalogs, local knowledge indexes, and verification reports.

Do not store raw CRM exports, full Gmail threads, phone/VIN/license tables,
cashbox ledgers, supplier credentials, OAuth state, or secrets in Git, manager
memory, or chat summaries.

## Startup Loop

1. For non-trivial owner requests, run one compact context command first:
   `python -m autostop_manager.cli agent-brief "<query>"` or
   `python -m autostop_manager.cli prepare-context "<query>"`.
2. For local knowledge/docs questions, run `knowledge-probe`. If it returns a
   route, open the returned `open_first` / source-of-truth files before broad
   reads.
3. For live CRM work, use the AutoStop CRM MCP connector. Start with
   `bootstrap_context` and `manager_board_scan`; use focused reads before heavy
   exports.
4. For Gmail work, follow `docs/agent/gmail_workflow_playbook.md`; read/search
   before any mailbox-changing action.
5. For broad CRM, procurement, finance, knowledge-intake, or multi-step work,
   create a manager run ledger entry and checkpoint compact progress.

## Write Safety

- Before CRM writes: exact target id, preflight/dry-run where available, reread
  after saving.
- Do not move, archive, delete, change deadlines/indicators, edit repair-order
  works/materials/prices/totals, payments, or cashboxes unless the owner gives
  a separate explicit command for that exact target.
- Use `prepare_crm_card_action` before card description or vehicle_profile
  writes orchestrated by AutostopManager.
- Public CRM card `description` create/update work must follow
  `docs/agent/crm_card_description_standard.md`: maximally laconic formatted
  working facts only, without risks, source/provenance, selection method, or
  supplier-check reminders.
- Gmail send/archive/delete/label/draft actions require explicit owner approval
  for the exact mailbox target.

## Standing Routes

| Owner intent | Open first | Boundary |
| --- | --- | --- |
| `Приберись` | `docs/agent/board_cleanup_autopilot_playbook.md` | Non-destructive card cleanup only; no movement/archive/finance/order writes without separate explicit command. |
| CRM card description create/update | `docs/agent/crm_card_description_standard.md` | Laconic rich-text working facts only; no source/provenance, selection method, risk/caveat blocks, or supplier-check reminders. |
| Ready unpaid / daily control | `docs/agent/krasnoyarsk_service_management_playbook.md` | Use live ready-unpaid CRM tools and dry-run followups first. |
| Timer floor | `docs/agent/crm_manager_data_playbook.md` | Use authenticated `bulk_set_deadline_if_below`, dry-run first, active cards only unless scope expands. |
| VIN/OEM/parts CRM writeback | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` | Never invent OEM numbers, applicability, stock, prices, or selected material rows. |
| Business documents | `docs/agent/business_document_quality_playbook.md` | AutoStop service documents use the CRM print module and standard templates. |
| Gmail | `docs/agent/gmail_workflow_playbook.md` | Read/search first; mutating actions require explicit approval. |
| Documentation hygiene | `docs/agent/knowledge_shelves.md` | Delete only fully migrated/obsolete files; update map/index/annotations and run audits. |

## Canonical Detail Files

- `docs/agent/autostop_manager_skill.md` - detailed startup behavior and route
  explanations.
- `docs/agent/manager_rules.json` - prioritized durable rules.
- `docs/agent/command_routes.json` - natural owner-command routes.
- `docs/agent/knowledge_base_index.md` - human knowledge navigation.
- `docs/agent/knowledge_shelves.md` - placement, deletion, source-pack policy.
- `docs/agent/crm_card_description_standard.md` - public CRM card description
  style for create/update/cleanup/writeback tasks.
- `docs/agent/knowledge_map.json` - machine route cards.
- `docs/agent/knowledge_annotations.jsonl` - compact file annotations.
- `docs/agent/manager_mcp_catalog.json` - AutostopManager MCP surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM MCP surface.

## Verification

- Docs/routing changes:
  `python -m autostop_manager.cli knowledge-sync`,
  `python -m autostop_manager.cli knowledge-audit`,
  `python -m autostop_manager.cli annotations-audit`,
  `python -m autostop_manager.cli skills-audit`.
- Code/contract changes: focused pytest first; full `python -m pytest -q` when
  shared behavior changed.
- Server/Codex readiness:
  `python -m autostop_manager.cli control-report --format markdown`.
