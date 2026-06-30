# AutostopManager Codex Instructions

This file is the Codex compatibility entrypoint. The canonical human-facing
startup file is `agent.md`; read it first for role, source boundaries, startup
loop, safety rules, standing routes, and verification.

Hard rules kept here so Codex sees them immediately:

- Answer the owner in Russian by default: short, operational, direct.
- AutoStop CRM is the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files, and live board state.
- Gmail is the source of truth for messages, threads, labels, drafts,
  attachments, and sent/archive history.
- AutostopManager stores only durable non-CRM memory, command routes, playbooks,
  compact catalogs, local knowledge indexes, and verification reports.
- This server has working reverse-SSH access to the owner's home Windows PC:
  use `ssh home-pc`; open `docs/agent/codex_home_pc_reverse_ssh.md` before
  interacting with or changing that route. `sftp`/`scp`, `pwsh`, and `python`
  are available there. Do not rotate its keys without updating Windows too.
- For non-trivial owner requests, start with
  `python -m autostop_manager.cli agent-brief "<query>"` or
  `python -m autostop_manager.cli prepare-context "<query>"`.
- For local knowledge/docs work, run `knowledge-probe` and open the returned
  source-of-truth files before broad reads.
- For live CRM work, use the AutoStop CRM MCP connector and start with
  `bootstrap_context` and `manager_board_scan`.
- Before CRM writes, identify the exact target id, prefer dry-run/preflight,
  and reread after saving.
- For public CRM card `description` create/update work, use
  `docs/agent/crm_card_description_standard.md`: maximally laconic rich-text
  working facts only; no risks, safety caveats, source/provenance, selection
  method, or supplier-check reminders in the card text.
- Use a manager run ledger for broad CRM, procurement, finance,
  knowledge-intake, documentation hygiene, or other multi-step work.
- Do not move, archive, delete, change deadlines/indicators, edit repair-order
  works/materials/prices/totals, payments, or cashboxes unless the owner gives a
  separate explicit command for that exact target.
- Canonical route highlights: `Приберись`, CRM card descriptions,
  `ready unpaid`, Timer floor,
  `home-pc` remote Codex access,
  `docs/agent/crm_vin_oem_parts_lookup_playbook.md`, and
  `docs/agent/business_document_quality_playbook.md`.
- After docs/routing changes run `knowledge-audit`, `annotations-audit`, and
  `skills-audit`.

Detailed docs: `agent.md`, `docs/agent/autostop_manager_skill.md`,
`docs/agent/manager_rules.json`, `docs/agent/command_routes.json`,
`docs/agent/crm_card_description_standard.md`,
`docs/agent/knowledge_base_index.md`,
`docs/agent/codex_home_pc_reverse_ssh.md`,
`docs/agent/manager_mcp_catalog.json`, and `docs/agent/crm_mcp_catalog.json`.
