# AutostopManager

Routing, knowledge and verified workflow logic for AutoStop. CRM, AutoStop App,
Gmail and Telegram keep their own live records; this repository keeps compact
rules and de-identified technical state.

## Start

- `AGENTS.md` — canonical startup contract.
- `docs/agent/command_routes.json` — operational routes.
- `docs/agent/knowledge_map.json` — domain navigation.
- `docs/agent/manager_rules.json` — cross-system invariants.
- `docs/agent/deployment_runbook.md` — verification, release and rollback.
- `.agents/skills/manage-autostop-store/SKILL.md` — Store and client quotes.

For non-trivial work run `agent-brief` and open only its route. `work` is the
default; explicit learning uses `autostop-learning-loop`.

```bash
.venv/bin/python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
.venv/bin/python -m autostop_manager.cli knowledge-probe "проверить документацию"
.venv/bin/python -m autostop_manager.cli control-report --format markdown
```

One file owns each rule: playbooks own procedures, routes own recognition,
navigation owns paths, and live registration owns schemas. Delete obsolete
material instead of keeping an active archive.
