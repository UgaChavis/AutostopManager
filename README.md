# AutostopManager

Manager memory, routing, knowledge, CRM/AutoStop App/Gmail workflow and
verification layer for AutoStop. Live business records stay in their owning
systems; this repository keeps only durable rules, compact references and
technical control logic.

## Start Here

- `AGENTS.md` - canonical compact startup instruction for Codex.
- `docs/agent/voice_agent_brief.md` - voice-only behavior delta loaded by
  `AGENTS.md`; it does not duplicate common safety or routing rules.
- `docs/agent/knowledge_map.json` - domain-to-file navigation.
- `docs/agent/command_routes.json` - operational command workflows.
- `docs/agent/manager_rules.json` - cross-system runtime invariants.
- `docs/agent/crm_mcp_catalog.json` - the 24-tool external Gateway manifest;
  `manager_mcp_catalog.json` - the 77-tool internal Manager registry manifest.
- `docs/agent/codex_home_pc_reverse_ssh.md` - managed Windows and legacy
  `home-pc` access.
- `docs/agent/remote_diagnostics_pad_vii_playbook.md` - supervised PAD VII
  diagnostic operator route and isolated MCP boundary.
- `docs/agent/deployment_runbook.md` - the only release checklist.
- `docs/agent/infrastructure_dependency_map.json` - compact dependency index;
  live state still requires the named readback.
- `.agents/skills/manage-autostop-store/SKILL.md` - guarded Store and quote
  handling with explicit live-capability gates; the detailed API contract
  remains in the Store playbook.

For non-trivial work run `agent-brief`, then open the returned canonical route.
`work` is the default; `learning` invokes the `autostop-learning-loop` skill.

Live systems own business data. Raw records, identifiers, ledgers, credentials
and private documents never enter Git or durable prompts.

## Common Commands

```bash
.venv/bin/python -m autostop_manager.cli agent-brief "Приберись" --intent board_cleanup
.venv/bin/python -m autostop_manager.cli knowledge-probe "проверить документацию"
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit
```

Integration audits include Store parity and read-only Store health checks by
default.

Runtime boundaries and write gates live in `AGENTS.md`; detailed procedures
live in the playbook selected by `agent-brief`.

## Quality And Documentation

The canonical verification, GitHub publication, deployment and rollback flow
is `docs/agent/deployment_runbook.md`. Documentation keeps one owner per rule:
procedures in playbooks, operational recognition in `command_routes.json`,
navigation in `knowledge_map.json`, and complete tool schemas in live
registration. Delete fully migrated files after `cleanup-audit` and green
knowledge audits; do not preserve obsolete plans as an active archive.
