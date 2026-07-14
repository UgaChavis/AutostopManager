# AutostopManager Codex Instructions

Canonical startup file for Codex in `/opt/AutostopManager`. Keep docs compact;
put detailed workflows in `docs/agent/*_playbook.md` and route metadata in
`docs/agent/knowledge_map.json`.

## Role

- Answer the owner in Russian by default: short, operational, direct.
- AutostopManager stores only durable non-CRM memory, routes, playbooks,
  compact catalogs, server checks, and verification.
- AutoStop CRM is the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and live board state.
- Gmail is the source of truth for messages, threads, labels, drafts,
  attachments, and sent/archive history.
- Do not store raw CRM exports, full Gmail threads, phone/VIN/license tables,
  cashbox ledgers, credentials, OAuth state, or secrets in docs, Git, memory, or
  chat summaries.
- Never dump `.env`, Docker `.Config.Env`, or a process environment. Inspect
  only an explicit allowlist of non-secret names and print secret presence or
  validation booleans, never values.

## Startup

1. For non-trivial owner requests, run one compact context command first:
   `python -m autostop_manager.cli agent-brief "<query>"` or
   `python -m autostop_manager.cli prepare-context "<query>"`.
2. For local knowledge/docs work, run `knowledge-probe "<query>"` and open the
   returned `open_first` / source-of-truth files before broad reads.
3. For live CRM work, use the AutoStop CRM MCP connector. Start with
   `agent_bootstrap`, then `agent_board_digest`; use `agent_search` and
   `agent_entity_context` for focused detail. Run broad control through
   `agent_board_workflow`, not the hidden legacy surface.
4. For Gmail work, open `docs/agent/gmail_workflow_playbook.md`; read/search
   before any mailbox-changing action.
5. For broad CRM, procurement, finance, knowledge-intake, or other multi-step
   work, use the Gateway v2 workflow ledger and compact state-versioned
   checkpoints. Use raw discovery only when no named workflow covers the task.

## Write Safety

- Before CRM writes: exact target id, dry-run/preflight where available, then
  reread and verify.
- For finance, inventory, documents, files, Gmail, or destructive writes, build
  the action contract, use a unique idempotency key, and keep any applied but
  unverified result in `compensating` until exact-target reconciliation.
- For card `description` or vehicle_profile writes, read the exact target with
  `agent_entity_context`, build `prepare_action_contract`, then use
  `agent_board_workflow(operation="cleanup_card")` in dry-run and apply modes.
- Public CRM card descriptions must follow
  `docs/agent/crm_card_description_standard.md`: laconic working facts only;
  no risks, provenance, selection method, supplier-check reminders, or long AI
  explanations.
- Do not move, archive, delete, change deadlines/indicators, edit repair-order
  rows/totals, payments, or cashboxes unless the owner gives a separate explicit
  command for that exact target.
- Gmail send/archive/delete/label/draft mutations require task-specific owner
  intent and an exact mailbox target. Agent Gateway v2 does not add a second
  confirmation state: after exact targets pass preflight, execute once with
  idempotency and record only message/thread/file refs in the run ledger.

## Standing Routes

- `Приберись` -> `docs/agent/board_cleanup_autopilot_playbook.md`.
- CRM card descriptions -> `docs/agent/crm_card_description_standard.md`.
- `ready unpaid` / daily control -> `docs/agent/krasnoyarsk_service_management_playbook.md`.
- Timer floor -> `docs/agent/crm_manager_data_playbook.md` and
  `agent_board_workflow(operation="bulk_set_deadline_if_below")`, dry-run first.
- VIN/OEM/parts CRM writeback -> `docs/agent/crm_vin_oem_parts_lookup_playbook.md`.
- Internet/repair web research -> CRM agent `search_web_multi` first
  (Brave -> Tavily -> Google CSE -> DuckDuckGo), then excerpt; use
  `fetch_page_browser` only for public JS-heavy pages. Do not bypass CAPTCHA,
  login, paywall, or IP blocks; report manual access needed.
- Business documents -> `docs/agent/business_document_quality_playbook.md`.
- Remote `home-pc` access -> `docs/agent/codex_home_pc_reverse_ssh.md`; use
  `ssh home-pc`, `sftp`/`scp`, `pwsh`, and Python only after opening that file.

## Documentation Hygiene

- Prefer updating the smallest existing canonical file over creating a new one.
- Delete obsolete tracked docs after their rules are migrated and
  `cleanup-audit` plus knowledge audits are green.
- After docs/routing/catalog changes run:
  `knowledge-sync`, `knowledge-audit`, `annotations-audit`, `skills-audit`;
  run `cleanup-audit` before deleting docs or generated artifacts.

## Core References

`README.md`, `docs/agent/autostop_manager_skill.md`,
`docs/agent/manager_rules.json`, `docs/agent/command_routes.json`,
`docs/agent/knowledge_base_index.md`, `docs/agent/knowledge_shelves.md`,
`docs/agent/manager_mcp_catalog.json`, `docs/agent/crm_mcp_catalog.json`.
