# AutostopManager

Manager memory, routing, knowledge, CRM/AutoStop App/Gmail workflow,
server-check, and verification layer for AutoStop. It does not replace the CRM,
store, or Gmail sources of truth.

## Start Here

- `AGENTS.md` - canonical compact startup instruction for Codex.
- `docs/agent/knowledge_shelves.md` - human navigation, file placement, and
  deletion policy.
- `docs/agent/codex_home_pc_reverse_ssh.md` - managed Windows fleet plus the
  independent legacy `home-pc` reverse SSH route.
- `docs/agent/knowledge_map.json` - machine route cards for `knowledge-probe`.
- `docs/agent/knowledge_annotations.jsonl` - compact file annotations.
- `docs/agent/command_routes.json` - standing owner-command routes.
- `docs/agent/manager_rules.json` - durable prioritized rules.
- `docs/agent/manager_mcp_catalog.json` - local manager MCP surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM MCP surface.
- `docs/agent/store_management_playbook.md` - AutoStop App reads, reliable
  change feed, optimized named workflows, and full guarded owner parity.

For each non-trivial request, the agent refreshes the relevant context first
and then chooses the smallest combination of live systems and sources that can
answer it: CRM, AutoStop App, Gmail, managed PC, local knowledge, VIN/OEM
catalogs, or public technical evidence. Automotive technical questions are not
bound to a fixed script: vehicle identity and the requested fact determine
whether to use an official repair source, public campaign/TSB evidence, a
catalog, forum evidence, or several of them together.

## Adaptive Manager and Learning Mode

The manager now treats a practical request as a set of claims rather than a
fixed command sequence. It can plan focused parallel reads, prioritize the
system that owns each fact, and reconcile compact evidence from CRM, AutoStop
App, Gmail, VIN/OEM catalogs, licensed sources, supplier data, official public
sources, and forums. CRM, Store, and Gmail records remain transient source
data; the Manager stores only safe technical experience and durable rules.

`work` is the default fast operational mode. `learning` adds an obligatory
post-run review: completion checks, tool/result assessment, a safe reusable
lesson or a bounded improvement candidate. A reproducible low-risk local
failure may be repaired only with a regression check and verification before
the answer; external failures, credentials, access controls, and financial
operations are never self-repaired. The canonical policy is
[`docs/agent/intelligent_agent_learning_playbook.md`](docs/agent/intelligent_agent_learning_playbook.md),
and the project-local execution skill is
[`autostop-learning-loop`](.agents/skills/autostop-learning-loop/SKILL.md).

## Daily Commands

```bash
.venv/bin/python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
.venv/bin/python -m autostop_manager.cli prepare-context "почисти документацию" --intent documentation_hygiene
.venv/bin/python -m autostop_manager.cli knowledge-probe "проверить Gmail коннектор почта ярлыки вложения"
.venv/bin/python -m autostop_manager.cli knowledge-search "счет НДС PDF render" --domain business_documents
.venv/bin/python -m autostop_manager.cli agent-mode status
.venv/bin/python -m autostop_manager.cli agent-mode set learning
.venv/bin/python -m autostop_manager.cli learning-summary
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit
.venv/bin/python -m autostop_manager.cli integration-audit --full
.venv/bin/python -m autostop_manager.cli crm-gateway-attest summary --run-id AST-GWAT-YYYYMMDDTHHMMSSZ
```

For a new non-trivial task: `agent-brief` or `prepare-context` first. For CRM:
live MCP context first. The public CRM connector exposes exactly 24 Gateway v2
tools over owner-approved OAuth 2.1; use `agent_bootstrap` ->
`agent_board_digest` -> `agent_search`/`agent_entity_context` ->
	`prepare_action_contract` -> named workflow `dry_run`/`apply` -> exact-target
reread. Raw discovery is allowed only when no named workflow exists; never call
a hidden legacy name directly. Semantic raw discovery returns read capabilities
only: for a needed raw write, discover its exact literal capability name, read
its live schema, then call it with an idempotency key. In particular,
`create_card` is not an `agent_board_workflow` operation; create the card
through this guarded route, reread it, and then use the same route for
`link_card_to_client` when an existing client/new vehicle must be linked. For
local docs: `knowledge-probe` first.
The integration audit runs both machine-verifiable capability matrices in
strict mode, so UI/API actions cannot silently lose Gateway coverage.

`agent-mode set work` switches the global default back to the fast operational
path; a per-turn override takes precedence. The hidden read-only
`agent_case_resolver` capability accepts only opaque case references and
compact scalar evidence, then returns a source-read plan or a reconciled,
redacted display conclusion. After publishing this checkout, trust the project
hooks once in Codex with `/hooks` so learning-mode Stop enforcement is active.

Employee/admin Store parity that has no named workflow is available only
through guarded raw `store_owner_capabilities` and `store_owner_api`. They load
the live OpenAPI operation schema and authenticate as the reserved
`store:owner` service principal via `AUTOSTOP_STORE_OWNER_TOKEN`; writes still
require schema-validated ActionContractV2 request fingerprints, exact refs,
backend-verified revision and dry-run receipt, idempotency, correlation, and an
operation-specific reread. Applied results cannot report completed before that
verification. This does not expand the public 24-tool surface.

For AutoStop App, the same 24 public Gateway tools gain backward-compatible
store scopes/entities: `agent_bootstrap`, `agent_board_digest(scope="store")`,
`agent_search`, `agent_entity_context`, `get_runtime_status`, and
`agent_inventory_workflow`. The Manager adapter calls only the pure-read
`/internal/agent/v1` API, never the store database or legacy GET routes with
side effects. Owner “what is new” reads use `store_digest`; `agent_bootstrap`
reads one stateless `/bootstrap-snapshot` response, so it has no cursor or ACK
and never touches any digest checkpoint. `agent_board_digest(scope="store")` keeps the
Manager-owned opaque cursor/ACK protocol and commits high-water only after the
final ACK.

## Core Audits

```bash
.venv/bin/python -m autostop_manager.cli knowledge-sync
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli cleanup-audit
.venv/bin/python -m autostop_manager.cli system-audit
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check autostop_manager tests
.venv/bin/python -m mypy autostop_manager
.venv/bin/python -m pytest -q
.venv/bin/python -m coverage run -m pytest -q
.venv/bin/python -m coverage report
node --check frontend/control-center/app.js
```

`system-audit` is read-only and reports test status as external; run pytest
separately after code, docs contracts, or route behavior changes. The coverage
gate measures branch coverage for the production package and currently requires
at least 82%. Ruff also enforces a complexity ceiling of 20 for business logic;
the flat CLI and MCP registration dispatchers are the only documented
declarative exceptions.

## Source Boundaries

- AutoStop CRM remains the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and board state.
- AutoStop App remains the source of truth for store catalog, physical/reserved/
  available stock, batches, storage locations, suppliers, quote requests,
  internet orders, warehouse operations, and marketplace state.
- Gmail remains the source of truth for mail, threads, labels, drafts,
  attachments, and sent history.
- AutostopManager stores only durable manager memory, routing rules, playbooks,
  technical store cursors, compact entity/version refs, compact catalogs, and
  local knowledge indexes.
- Never commit raw CRM/store exports, store orders/customer contacts/line items/
  stock rows, Gmail bodies, private business requisites, SQLite databases,
  OAuth state, supplier credentials, or generated PDFs unless the owner
  explicitly promotes a safe artifact.

## Main Routes

| Task | Open first |
| --- | --- |
| Startup / identity | `AGENTS.md` |
| Adaptive execution / learning mode | `docs/agent/intelligent_agent_learning_playbook.md` |
| Knowledge/docs hygiene | `docs/agent/knowledge_shelves.md` |
| CRM manager data summaries | `docs/agent/crm_manager_data_playbook.md` |
| Store analytics | `docs/agent/store_analytics_playbook.md` |
| AutoStop App store | `docs/agent/store_management_playbook.md` |
| `Приберись` | `docs/agent/board_cleanup_autopilot_playbook.md` |
| Gmail | `docs/agent/gmail_workflow_playbook.md` |
| Business documents | `docs/agent/business_document_quality_playbook.md` |
| VIN/OEM/parts writeback | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` |
| Vehicle identity / OEM | `docs/agent/vehicle_identity_playbook.md` |
| Parts sourcing | `docs/agent/parts_search_playbook.md` |
| Automotive repair, fluids, public technical evidence, and web research | `docs/agent/automotive_repair_source_playbook.md` |
| Service management | `docs/agent/krasnoyarsk_service_management_playbook.md` |
| Remote Codex access / `managed-pc` / `home-pc` | `docs/agent/codex_home_pc_reverse_ssh.md` |
| Deployment | `docs/agent/deployment_runbook.md` |

## Documentation Hygiene

Keep procedures in playbooks, route metadata in
`docs/agent/knowledge_map.json`, and short search summaries in
`docs/agent/knowledge_annotations.jsonl`. Keep large source packs cold:
README/MANIFEST, source/license notes, and important structured tables only.
Prefer updating an existing canonical file over creating a new one. Delete only
fully migrated/obsolete tracked docs after `cleanup-audit` and green knowledge
audits.

When reorganizing docs, update the smallest canonical file, then run:

```bash
.venv/bin/python -m autostop_manager.cli knowledge-sync
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli cleanup-audit
```
