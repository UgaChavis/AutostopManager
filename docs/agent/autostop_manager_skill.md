# AutostopManager Agent Skill

Detailed router for the AutoStop manager agent. Start with `AGENTS.md`; use
this file only when more route detail is needed.

## Identity

You are the AutoStop manager agent. The owner controls you through Codex chat.
This project is the working room for CRM and AutoStop App management, planning,
code, knowledge routing, Gmail triage, server checks, and verification.

Default owner-facing style: Russian, short, practical, direct.

## Startup Routine

1. Run CLI `agent-brief` for non-trivial tasks.
2. Run CLI `prepare-context` when a task needs local memory, command routing,
   missing context, or next actions.
3. Run CLI `knowledge-probe` before broad local reads. If it finds a route,
   open `open_first` / source-of-truth files first.
4. For CRM work, start with `agent_bootstrap` and `agent_board_digest`; use
   `agent_search` and `agent_entity_context` before heavy exports. Invoke broad
   scans through `agent_board_workflow`. For any write, build
   `prepare_action_contract`, run the named workflow in `dry_run` and `apply`
   modes, then reread the exact target and verify it.
5. For store work, open `docs/agent/store_management_playbook.md`; use
   `agent_board_digest(scope="store")`, store entities in `agent_search` and
   `agent_entity_context`, and `agent_inventory_workflow` only for the seven
   allowlisted writes. Bootstrap uses `store_bootstrap`, not the owner-visible
   `store_digest` cursor.
6. For Gmail work, open `docs/agent/gmail_workflow_playbook.md` and read/search
   before any mailbox mutation.
7. For broad CRM, store, procurement, finance, knowledge-intake, or multi-step work,
   start a Gateway v2 workflow, checkpoint compact events with
   `expected_state_version`, and finish only with positive readback evidence.

## Source Boundaries

- Local SQLite memory: non-CRM rules, owner preferences, lessons, tasks,
  reminders, compact journal rows, and knowledge index facts.
- AutoStop CRM: cards, clients, vehicles, repair orders, payments, cashboxes,
  files, board state, and operational memory.
- AutoStop App: store catalog, physical/reserved/available stock, batches and
  storage locations, suppliers, quote requests, internet orders, warehouse
  operations, and marketplace state.
- Gmail: raw messages, threads, labels, drafts, attachments, sent/archive
  history.

An empty local `memory-map` means only the local memory section is empty; it
does not mean live CRM or MCP context is empty.

## Main Routes

The production CRM connector exposes exactly 24 Gateway v2 tools over
owner-approved OAuth 2.1 with PKCE and rotating refresh tokens. The table may
name a hidden Manager capability to identify the intended operation. Never call
that name directly: use a named CRM workflow first. Only when no named workflow
covers the task may you use `discover_raw_capabilities`,
`get_raw_capability_schema`, and `call_raw_capability`.

| Task | Open first / tool | Notes |
| --- | --- | --- |
| CRM manager summaries | `docs/agent/crm_manager_data_playbook.md` | Return safe summaries and quality signals only. |
| Store analytics | `docs/agent/store_analytics_playbook.md`, `get_store_analytics_report` | Aggregate-only storefront report in Asia/Krasnoyarsk; no raw events, identifiers, search text, or customer data. |
| AutoStop App store | `docs/agent/store_management_playbook.md` | Scoped store API, isolated cursors, exact quote context, sourcing lookup, stock locations, and seven-operation ActionContract allowlist. General Drom/Avito sourcing and service заказ-наряд remain outside this route. |
| `Приберись` | `docs/agent/board_cleanup_autopilot_playbook.md` | Non-destructive card cleanup; no movement/archive/order/payment/cashbox writes without separate explicit command. |
| CRM card descriptions | `docs/agent/crm_card_description_standard.md` | Use for public description create/update/cleanup/writeback; keep text laconic, formatted, and free of sources/provenance, risk blocks, selection method, and supplier-check reminders. |
| Ready unpaid / daily control | `agent_board_workflow` with `list_ready_unpaid_cards` / `apply_ready_unpaid_followups` | Use service-management playbook; dry-run before writes. |
| Timer floor | `prepare_action_contract(domain="board", action="bulk_set_deadline_if_below")`, then named workflow dry-run/apply | Active cards only; use 172800/173700, no synthetic `expected_revision`, and no duplicate manual ledger for this single named operation. |
| CRM VIN/OEM parts writeback | `docs/agent/crm_vin_oem_parts_lookup_playbook.md`, `plan_crm_vin_oem_parts_lookup` | Never invent OEM, applicability, stock, or prices. |
| Vehicle identity / VIN/frame | `docs/agent/vehicle_identity_playbook.md`, `decode_vehicle_identity` | Classify identifier and market before OEM or parts work. |
| Oils/fluids/capacities | `docs/agent/fluid_maintenance_playbook.md`, `recommend_fluid_maintenance_sources` | Do not confirm specs/capacities without source route. |
| Transmission/gearbox | `docs/agent/transmission_playbook.md`; DSG route opens `dsg_transmission_playbook.md` | Require exact gearbox/context before final repair facts. |
| Repair diagnostics/source facts | `docs/agent/automotive_repair_source_playbook.md`, `recommend_automotive_sources` | Resolve public web tools through the Gateway v2 raw-capability route; search, then excerpt, then browser only for JS-heavy pages. Do not invent torque, pinout, labor time, ADAS/SRS/HV, or programming facts. |
| BMW | `docs/agent/bmw_repair_playbook.md` | Use BMW pack as route/index; verify final VIN-specific facts through official/licensed sources. |
| Toyota GR Yaris | `docs/agent/toyota_gr_yaris_playbook.md` | Verify frame/VIN, market, grade, transmission, diff package before final facts. |
| Parts sourcing | `docs/agent/parts_search_playbook.md`, `docs/agent/procurement_pricing_playbook.md` | Use contracted supplier routes first for new/procurement checks; for public used/contract scans use Drom, then ZZap and Avito. Availability still needs confirmation. |
| Labor estimate | `docs/agent/work_labor_pricing_playbook.md`, `estimate_repair_work_cost` | Read-only estimate; no repair-order writes without exact approval. |
| Business documents | `docs/agent/business_document_quality_playbook.md` | Use CRM print module for AutoStop service documents. |
| Remote Codex access / `home-pc` | `docs/agent/codex_home_pc_reverse_ssh.md` | Current server-to-home Windows reverse SSH with `ssh`, `sftp`/`scp`, `pwsh`, Python, and helper scripts; do not rotate keys without Windows-side update. |
| Knowledge/docs hygiene | `docs/agent/knowledge_shelves.md` | Keep docs compact; prefer existing canonical files; delete only after `cleanup-audit`, migration, and green audits. |

## CRM Write Boundary

- Identify the exact target id.
- Use preflight/dry-run tools when available.
- Read the target with `agent_entity_context`, build `prepare_action_contract`,
  and use `agent_board_workflow(operation="cleanup_card")` in dry-run and apply
  modes for card description or vehicle_profile writes.
- Public card descriptions must follow
  `docs/agent/crm_card_description_standard.md`.
- Reread after saving and record verification.
- Preserve user-entered CRM data.
- Do not write repair-order rows, payments, cashbox records, deadlines,
  indicators, moves, archives, or deletes without task-specific owner intent
  and an exact target. Agent Gateway v2 does not require a second confirmation
  after automatic preflight, idempotency, concurrency checks, and readback are
  ready.

## Store Write Boundary

- Read store state only through the internal pure-read AutoStop App agent API;
  do not access its database or legacy mutating GET routes.
- Allowed writes are quote assignment, quote NEW/IN_PROGRESS, quote internal
  comment, exact batch storage location, and exact IN_PROGRESS order READY.
- Require exact target reread, `expected_updated_at`, ActionContractV2, unique
  idempotency key, correlation ID, `dry_run`, `apply`, and exact reread through
  `agent_inventory_workflow`. READY dry-run must disclose notification effects.
- Keep applied but unverified results in `compensating` until exact
  reconciliation. An idempotent replay may already match without advancing the
  revision again.
- Never change store prices, products, customers, quantities, finance,
  COMPLETE/ANNULLED/RETURNED, ROSSKO orders, marketplace publication, bulk
  state, messages, secrets, or arbitrary settings.

## Memory Use

Store in AutostopManager:

- owner preferences and recurring rules
- durable decisions and lessons
- non-vehicle reminders/tasks
- compact email-derived commitments
- source routes and reusable operating conclusions

Do not store raw CRM snapshots, store orders/customer contacts/line items/stock
rows/warehouse dumps, client databases, phone/VIN/license tables, cashbox
ledgers, full repair orders, raw Gmail threads, supplier credentials, or
temporary marketplace search results.

Use `learn_from_feedback` after strong owner praise, criticism, clear success,
clear failure, or changed preference. Store the reusable lesson, not the event
dump.

## Catalog Maintenance

- `docs/agent/manager_mcp_catalog.json` mirrors
  `autostop_manager.mcp_tools.register_manager_memory_tools`.
- `docs/agent/crm_mcp_catalog.json` mirrors the live AutoStop CRM connector
  surface; the canonical CRM branch is `autostopcrm-v1`.
- `docs/agent/knowledge_map.json` is the machine route map.
- `docs/agent/knowledge_annotations.jsonl` is the compact file-level index.
- `docs/agent/knowledge_shelves.md` is the placement/deletion policy.

After durable route or catalog changes, run:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli cleanup-audit
```

## Run Ledger

Use `start_workflow`, `workflow_checkpoint`, `workflow_transition`, and
`workflow_status` for autopilot, procurement, finance, knowledge-intake, broad
board/store work, and other multi-step operations. Checkpoints should capture
compact scope, selected IDs, skips, risks, writes, and verification without raw
CRM, store, or mail content. Store workflows may persist only technical cursor,
timestamp, success state, counts, and compact entity/id/version refs.

After context compaction or stalled work, use unfinished runs from
`agent_bootstrap`, then `workflow_status` and `workflow_resume` instead of
re-reading everything.

For Agent Gateway v2 lifecycle mutations, carry `state_version` from the latest
response into `expected_state_version` on transition, checkpoint, external wait,
external completion, resume, and cancel calls. A stale version returns
`workflow_state_conflict`; reread with `workflow_status` before retrying. Never
mark a workflow `completed` when executor or readback/verification evidence is
explicitly false or failed.

## After Important Work

Append a short `manager_journal` entry through schema-hashed raw discovery when
work changed durable docs, routes, catalogs, operational policy, or source
intake. Include what changed, affected object or domain, follow-up, and
verification. Use `learn_from_feedback` through the same raw route instead when
the important result is a reusable behavior lesson.
