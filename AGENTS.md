# AutostopManager Codex Instructions

Canonical startup file for Codex in `/opt/AutostopManager`. Keep docs compact;
put detailed workflows in `docs/agent/*_playbook.md` and route metadata in
`docs/agent/knowledge_map.json`.

## Role

- Answer the owner in Russian by default: short, operational, direct.
- AutostopManager stores only durable non-CRM memory and no raw store data:
  routes, playbooks, technical cursors, compact refs/catalogs, server checks,
  and verification.
- AutoStop CRM is the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and live board state.
- AutoStop App is the source of truth for the store catalog, stock, batches,
  storage locations, suppliers, quote requests, internet orders, warehouse
  operations, and marketplace state.
- Gmail is the source of truth for messages, threads, labels, drafts,
  attachments, and sent/archive history.
- Do not store raw CRM/store exports, full store orders/stock rows, full Gmail
  threads, phone/VIN/license tables, cashbox ledgers, credentials, OAuth state,
  or secrets in docs, Git, memory, workflow state, or chat summaries.
- Never dump `.env`, Docker `.Config.Env`, or a process environment. Inspect
  only an explicit allowlist of non-secret names and print secret presence or
  validation booleans, never values.

## Startup

1. For non-trivial owner requests, run one compact context command first:
   `.venv/bin/python -m autostop_manager.cli agent-brief "<query>"` or
   `.venv/bin/python -m autostop_manager.cli prepare-context "<query>"` on the
   production Linux checkout. Use the active project venv equivalent on other
   platforms; do not fall back to the host system Python.
2. For local knowledge/docs work, run `knowledge-probe "<query>"` and open the
   returned `open_first` / source-of-truth files before broad reads.
3. For live CRM work, use the AutoStop CRM MCP connector. Start with
   `agent_bootstrap`, then `agent_board_digest`; Store bootstrap is one
   stateless snapshot request with no cursor/ACK. Use `agent_search` and
   `agent_entity_context` for focused detail. Run broad control through
   `agent_board_workflow`, not the hidden legacy surface.
4. For store work, open `docs/agent/store_management_playbook.md`; use existing
   Gateway tools with store scope/entities. Bootstrap uses `store_bootstrap`;
   owner “what is new” reads use `store_digest`. Never call the store DB or
   legacy GET routes with side effects. Human-UI parity operations without a
   named workflow may use guarded raw `store_owner_capabilities` and
   `store_owner_api`; they require the reserved `store:owner` service principal
   through `AUTOSTOP_STORE_OWNER_TOKEN` and the live OpenAPI operation schema.
5. For Gmail work, open `docs/agent/gmail_workflow_playbook.md`; read/search
   before any mailbox-changing action.
6. For automotive technical questions, start with the returned knowledge route,
   then select only the capabilities needed by the actual question: CRM for an
   identified live card or vehicle, AutoStop App for internal catalog/stock/price,
   VIN/OEM sources for identity and applicability, official public evidence for
   recalls/communications, and public web/forums for research. Treat this as
   adaptive source selection, not a fixed workflow; final safety, procedure,
   torque, fluid, programming, and exact-fitment facts need an appropriate
   OEM/licensed source and vehicle/unit context.
7. For broad CRM, store, procurement, finance, knowledge-intake, or other multi-step
   work, use the Gateway v2 workflow ledger and compact state-versioned
   checkpoints. Use `discover_raw_capabilities` ->
   `get_raw_capability_schema` -> `call_raw_capability` only when no named
   workflow covers the task; never invoke a hidden capability directly.
8. Resolve the effective `work`/`learning` mode before a non-trivial task. In
   `learning`, use the project `autostop-learning-loop` skill and close the
   post-run review before the final answer. Open
   `docs/agent/intelligent_agent_learning_playbook.md` for the policy.

The production connector must expose exactly 24 Gateway v2 tools. Codex/Apps
authenticate through owner-approved OAuth 2.1 with PKCE and rotating refresh
tokens; the deployment-rotated bearer is internal compatibility only. For a
write, the mandatory order is focused reread -> `prepare_action_contract` ->
named workflow `dry_run` -> named workflow `apply` -> exact-target reread and
verification.

## Write Safety

- The owner's active task authorizes the non-financial exact-target changes
  needed to complete it. Choose scope intelligently, but never expand into
  unrelated cleanup; retain preflight, idempotency, concurrency, and readback.
- Payments, cashboxes, refunds, payroll payouts, supplier orders, and any
  change to a financial total require a direct owner instruction for that
  exact operation, even in `learning` mode.
- Before CRM writes: exact target id, dry-run/preflight where available, then
  reread and verify.
- Seven common quote/batch/READY writes remain optimized named operations in
  `agent_inventory_workflow`; their existing strict DTO and notification rules
  remain in force. Every other employee/admin Store action exposed by the live
  OpenAPI is available only through guarded `store_owner_api` and the reserved
  `store:owner` principal. Exact full quote reads still use the dedicated
  quote-scoped credential when the named read path is sufficient.
- Broader Store owner operations must be necessary to the active owner task and
  use an exact OpenAPI operation and target refs, ActionContractV2,
  dry-run/preflight, unique idempotency and correlation IDs, then exact reread.
  High-risk apply also requires the matching dry-run proof; unresolved outcomes
  remain `compensating`.
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
- Move, archive, delete, change deadlines/indicators, or edit a repair order
  only when it is necessary to the active task and the exact target passes the
  normal safeguards. Financial repair-order changes still need direct owner
  instruction.
- Gmail send/archive/delete/label/draft mutations may be performed when needed
  to complete the active owner task and an exact mailbox target is resolved.
  After preflight, execute once with idempotency and record only
  message/thread/file refs in the run ledger.

## Standing Routes

- `Приберись` -> `docs/agent/board_cleanup_autopilot_playbook.md`.
- CRM card descriptions -> `docs/agent/crm_card_description_standard.md`.
- `ready unpaid` / daily control -> `docs/agent/krasnoyarsk_service_management_playbook.md`.
- Timer floor -> `docs/agent/crm_manager_data_playbook.md` and
  `prepare_action_contract(domain="board", action="bulk_set_deadline_if_below")`,
  then `agent_board_workflow(operation="bulk_set_deadline_if_below")`, dry-run
  first. This collection action does not require `expected_revision`.
- VIN/OEM/parts CRM writeback -> `docs/agent/crm_vin_oem_parts_lookup_playbook.md`.
- Store analytics questions -> `docs/agent/store_analytics_playbook.md` and the
  aggregate-only `get_store_analytics_report` capability through Gateway v2 raw
  discovery. Never request or persist raw events or visitor/session ids.
- Store state/catalog/stock/orders/quotes/marketplace/full owner operations ->
  `docs/agent/store_management_playbook.md`. General Drom/Avito sourcing stays
  in the parts route; service `заказ-наряд` stays in CRM.
- Internet/repair web research -> resolve `search_web_multi`, excerpt, and
  `fetch_page_browser` through `discover_raw_capabilities` ->
  `get_raw_capability_schema` -> `call_raw_capability`. Search first; use the
  browser only for public JS-heavy pages. Do not bypass CAPTCHA, login, paywall,
  or IP blocks; report manual access needed.
- Business documents -> `docs/agent/business_document_quality_playbook.md`.
- Remote Windows access -> `docs/agent/codex_home_pc_reverse_ssh.md`; keep the
  `managed-pc` fleet and legacy `home-pc` route independent, resolve the exact
  device, and run the documented status check before an operation.

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
`docs/agent/knowledge_shelves.md`,
`docs/agent/manager_mcp_catalog.json`, `docs/agent/crm_mcp_catalog.json`.
