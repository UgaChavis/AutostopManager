# AutostopManager

Manager memory, routing, knowledge, CRM/AutoStop App/Gmail workflow and
verification layer for AutoStop. Live business records stay in their owning
systems; this repository keeps only durable rules, compact references and
technical control logic.

## Start Here

- `AGENTS.md` - canonical compact startup instruction for Codex.
- `docs/agent/voice_agent_brief.md` - voice-only behavior delta loaded by
  `AGENTS.md`; it does not duplicate common safety or routing rules.
- `docs/agent/service_director_manifest.md` - autonomous recurring workshop
  director cycle, priorities and internal Telegram follow-up.
- `docs/agent/knowledge_shelves.md` - human route and deletion policy.
- `docs/agent/knowledge_map.json` - machine route cards.
- `docs/agent/command_routes.json` - standing owner commands.
- `docs/agent/manager_rules.json` - durable prioritized rules.
- `docs/agent/manager_mcp_catalog.json` and `crm_mcp_catalog.json` - verified
  tool-surface mirrors.
- `docs/agent/codex_home_pc_reverse_ssh.md` - managed Windows and legacy
  `home-pc` access.
- `docs/agent/deployment_runbook.md` - the only release checklist.

For non-trivial work run `agent-brief` or `prepare-context`, then open the
returned canonical route. The Manager chooses only the live systems and
evidence needed by the question. `work` is the default mode; `learning` adds
the review in `intelligent_agent_learning_playbook.md`.

CRM owns service cards, clients, vehicles, orders and money; AutoStop App owns
store catalog, stock, suppliers and marketplace state; Gmail owns mail. Raw
records, contacts, VIN/plate tables, ledgers, credentials, OAuth state, SQLite
data and generated private documents never enter Git or durable prompts.

## Common Commands

```bash
.venv/bin/python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
.venv/bin/python -m autostop_manager.cli knowledge-probe "проверить документацию"
.venv/bin/python -m autostop_manager.cli knowledge-search "счет НДС PDF" --domain business_documents
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit --full
```

Public CRM exposes exactly 24 Gateway v2 tools. Reads use bootstrap/digest and
focused context; writes require ActionContractV2, dry-run where supported,
idempotent apply and exact reread. Hidden capabilities require literal-name,
schema-bound raw discovery when no named workflow exists.

Store safety, cursors and guarded owner parity live only in
`docs/agent/store_management_playbook.md`. Automotive source choice is
adaptive: vehicle identity and the requested fact decide whether to use CRM,
VIN/OEM, licensed service data, public communications or bounded research.

## Quality And Documentation

The canonical verification, GitHub publication, deployment and rollback flow
is `docs/agent/deployment_runbook.md`. Documentation keeps one owner per rule:
procedures in playbooks, routes in `knowledge_map.json`, short search summaries
in `knowledge_annotations.jsonl`, and source lists in structured catalogs.
Delete fully migrated files after `cleanup-audit` and green knowledge audits;
do not preserve obsolete plans as an active archive.
