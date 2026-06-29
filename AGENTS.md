# AutostopManager Codex Instructions

## Role

This repository is the AutoStop manager control room for Codex. Use it for
manager memory, routing rules, knowledge indexes, CRM/Gmail workflows, server
checks, and verification. It does not replace AutoStop CRM or Gmail.

Default answer style for owner-facing work: Russian, short, operational, and
direct.

## Sources Of Truth

- AutoStop CRM is the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and live board state.
- Gmail is the source of truth for mail, threads, labels, drafts, attachments,
  and sent/archive history.
- AutostopManager stores durable manager memory, command routes, playbooks,
  compact catalogs, and local knowledge indexes only.
- Do not store raw CRM exports, full Gmail threads, phone lists, VIN/license
  tables, cashbox ledgers, supplier credentials, OAuth state, or secrets in
  Git, durable memory, or chat summaries.

## Startup Flow

- For non-trivial owner requests, start with the compact manager context:
  `python -m autostop_manager.cli agent-brief "<query>"` or
  `python -m autostop_manager.cli prepare-context "<query>"`.
- For local knowledge questions, run `knowledge-probe` first. If it finds a
  route, open the returned source-of-truth file before broad reads.
- For live CRM work, use the AutoStop CRM MCP connector and start with
  `bootstrap_context` and `manager_board_scan`; use focused reads before heavy
  board exports.
- For Gmail work, follow `docs/agent/gmail_workflow_playbook.md` and read/search
  before any mailbox-changing action.

## CRM Safety

- Before CRM writes, identify the exact target by id, prefer dry-run/preflight
  tools, and reread after saving.
- For multi-step CRM, procurement, finance, knowledge-intake, or broad board
  work, create a manager run ledger entry, record compact checkpoints, and
  finish with verification evidence.
- Do not move, archive, delete, change deadlines/indicators, edit repair-order
  works/materials/prices/totals, payments, or cashboxes unless the owner gives
  a separate explicit command for that exact target.
- Use `prepare_crm_card_action` before card description or vehicle_profile
  writes when AutoStopManager orchestrates the change.

## Canonical Routes

- `Приберись`: follow `docs/agent/board_cleanup_autopilot_playbook.md`; this is
  non-destructive card cleanup, not card movement or archive.
- Ready unpaid / daily control (`ready unpaid`): use
  `docs/agent/krasnoyarsk_service_management_playbook.md` and live CRM ready
  unpaid tools.
- Timer floor work: use `bulk_set_deadline_if_below` through the authenticated
  CRM MCP/runtime path, dry-run first, active cards only unless the owner
  expands scope.
- VIN/OEM/parts writeback: follow
  `docs/agent/crm_vin_oem_parts_lookup_playbook.md`; never invent OEM numbers,
  applicability, stock, or prices.
- Business documents: follow
  `docs/agent/business_document_quality_playbook.md`; AutoStop service
  documents must use the CRM print module and standard AutoStop templates.

## Verification

- After docs/routing changes, run:
  `python -m autostop_manager.cli knowledge-audit`,
  `python -m autostop_manager.cli annotations-audit`, and
  `python -m autostop_manager.cli skills-audit`.
- After code or contract changes, run focused pytest first; use full
  `python -m pytest -q` when behavior or shared contracts changed.
- Server/Codex readiness is summarized by
  `python -m autostop_manager.cli control-report --format markdown`.

## Canonical Detailed Docs

- `README.md` - project navigation and common commands.
- `docs/agent/autostop_manager_skill.md` - detailed startup routine, identity,
  memory boundaries, and operating rules.
- `docs/agent/manager_rules.json` - durable prioritized operating rules.
- `docs/agent/command_routes.json` - natural owner-command routes.
- `docs/agent/knowledge_base_index.md` - local knowledge entrypoint.
- `docs/agent/manager_mcp_catalog.json` - AutostopManager MCP surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM MCP surface.
