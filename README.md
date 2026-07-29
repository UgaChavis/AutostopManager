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

## Operating Model

The Manager resolves a request as a set of claims, reads each fact from its
owning system, and returns one compact conclusion. `work` is the default mode;
`learning` adds the review defined in
[`intelligent_agent_learning_playbook.md`](docs/agent/intelligent_agent_learning_playbook.md)
and the project-local
[`autostop-learning-loop`](.agents/skills/autostop-learning-loop/SKILL.md).
Live CRM, Store, and Gmail records remain transient; only safe technical
experience, compact references, routes, and durable rules belong here.

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

For a non-trivial task use `agent-brief` or `prepare-context` first; for local
documentation use `knowledge-probe`. The public CRM connector remains exactly
24 Gateway v2 tools. Reads start with bootstrap/digest and focused entity
context; writes require ActionContractV2, dry-run where available, apply, and
exact reread. Hidden capabilities are reached only through schema-bound raw
discovery when no named workflow exists.

Store reads use the internal pure-read API. Named store actions use
`agent_inventory_workflow`; remaining employee parity stays behind
`store_owner_capabilities` and `store_owner_api` with the reserved
`store:owner` principal. The canonical safety and cursor rules live in
[`store_management_playbook.md`](docs/agent/store_management_playbook.md).

## Quality and Release

The single canonical verification and deployment checklist is
[`deployment_runbook.md`](docs/agent/deployment_runbook.md). It includes
knowledge audits, Ruff, format verification, Mypy, pytest, branch coverage, and
frontend syntax validation. Coverage must remain at least 82%; business logic
has a Ruff complexity ceiling of 20, excluding only the documented flat CLI and
MCP registration dispatchers.

## Source Boundaries

CRM owns service operations, AutoStop App owns store operations, and Gmail owns
mail. AutostopManager stores only durable rules, playbooks, compact references,
technical cursors, catalogs, and local indexes. Raw operational records,
customer identifiers, money journals, credentials, OAuth state, SQLite data,
and generated private documents never enter Git.

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

After reorganizing documentation, run the canonical verification block in the
deployment runbook.
