# AutostopManager

Manager memory, routing, knowledge, CRM/Gmail workflow, server-check, and
verification layer for AutoStop. It does not replace AutoStop CRM or Gmail.

## Start Here

- `AGENTS.md` - canonical compact startup instruction for Codex.
- `docs/agent/architecture.md` - runtime boundaries, code layers, and contracts.
- `docs/agent/security.md` - trust boundaries, write protocol, and data policy.
- `docs/agent/development.md` - supported Python, locked install, and quality gates.
- `docs/agent/deployment_runbook.md` - backup, deploy, smoke, and rollback procedure.
- `docs/agent/knowledge_base_index.md` - compact human navigation.
- `docs/agent/knowledge_shelves.md` - file placement and deletion policy.
- `docs/agent/codex_home_pc_reverse_ssh.md` - `home-pc` reverse SSH access,
  toolset, and helper workflow.
- `docs/agent/knowledge_map.json` - machine route cards for `knowledge-probe`.
- `docs/agent/knowledge_annotations.jsonl` - compact file annotations.
- `docs/agent/command_routes.json` - standing owner-command routes.
- `docs/agent/manager_rules.json` - durable prioritized rules.
- `docs/agent/manager_mcp_catalog.json` - local manager MCP surface.
- `docs/agent/crm_mcp_catalog.json` - AutoStop CRM MCP surface.

## Daily Commands

```powershell
python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
python -m autostop_manager.cli prepare-context "почисти документацию" --intent documentation_hygiene
python -m autostop_manager.cli knowledge-probe "проверить Gmail коннектор почта ярлыки вложения"
python -m autostop_manager.cli knowledge-search "счет НДС PDF render" --domain business_documents
python -m autostop_manager.cli control-report --format markdown
```

For a new non-trivial task: `agent-brief` or `prepare-context` first. For CRM:
live MCP context first. For local docs: `knowledge-probe` first.

## Core Audits

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check autostop_manager tests
.venv/bin/mypy autostop_manager
.venv/bin/python -m pytest -q
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/pip-audit --progress-spinner off
.venv/bin/vulture autostop_manager --min-confidence 80
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli cleanup-audit
python -m autostop_manager.cli system-audit
```

`system-audit` is read-only and reports test status as external; run pytest
separately after code, docs contracts, or route behavior changes.

Install the reproducible environment from `requirements.lock` with
`pip install --require-hashes -r requirements.lock`, then install this package
editable with `pip install --no-deps -e .`. CI runs the same gates on Python
3.11 and 3.12.

## Source Boundaries

- AutoStop CRM remains the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and board state.
- Gmail remains the source of truth for mail, threads, labels, drafts,
  attachments, and sent history.
- AutostopManager stores only durable manager memory, routing rules, playbooks,
  compact catalogs, and local knowledge indexes.
- Never commit raw CRM exports, Gmail bodies, private business requisites,
  SQLite databases, OAuth state, supplier credentials, or generated PDFs unless
  the owner explicitly promotes a safe artifact.

## Main Routes

| Task | Open first |
| --- | --- |
| Startup / identity | `AGENTS.md` |
| Broad project audit/refactor | `docs/agent/architecture.md` |
| Development and tests | `docs/agent/development.md` |
| Security/data policy | `docs/agent/security.md` |
| Knowledge/docs hygiene | `docs/agent/knowledge_shelves.md` |
| CRM manager data summaries | `docs/agent/crm_manager_data_playbook.md` |
| `Приберись` | `docs/agent/board_cleanup_autopilot_playbook.md` |
| Gmail | `docs/agent/gmail_workflow_playbook.md` |
| Business documents | `docs/agent/business_document_quality_playbook.md` |
| VIN/OEM/parts writeback | `docs/agent/crm_vin_oem_parts_lookup_playbook.md` |
| Vehicle identity / OEM | `docs/agent/vehicle_identity_playbook.md` |
| Parts sourcing | `docs/agent/parts_search_playbook.md` |
| Internet / repair web research | `docs/agent/automotive_repair_source_playbook.md` |
| Service management | `docs/agent/krasnoyarsk_service_management_playbook.md` |
| Remote Codex access / `home-pc` | `docs/agent/codex_home_pc_reverse_ssh.md` |
| Deployment | `docs/agent/deployment_runbook.md` |

The semantic router scores intent, object, action, source, expected output,
read/write boundary, risk, applicability, negative evidence, and confidence.
Low-confidence or mixed broad requests use the safe `project_maintenance`
route or return ambiguity instead of selecting a narrow playbook by one shared
keyword.

## Documentation Hygiene

Keep procedures in playbooks, route metadata in `knowledge_map.json`, and short
search summaries in `knowledge_annotations.jsonl`. Keep large source packs cold:
README/MANIFEST, source/license notes, and important structured tables only.
Prefer updating an existing canonical file over creating a new one. Delete only
fully migrated/obsolete tracked docs after `cleanup-audit` and green knowledge
audits.

When reorganizing docs, update the smallest canonical file, then run:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli cleanup-audit
```
