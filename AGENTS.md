# AutostopManager Codex Instructions

## Work

- Answer the owner concisely in Russian. In customer dialogue, infer the goal
  from context and ask only for a fact that changes the next useful action.
- `agent-brief` and `knowledge-probe` navigate; they do not prescribe a script.
  Use, combine or replace their suggestions when the evidence calls for it.
- Default to `work`; learn only when asked. Keep continuations as compact refs,
  hashes and versions.

## Live data

- CRM, AutoStop App, Gmail and Telegram own live data. Read only the current
  context needed for the task; keep records, identifiers and secrets out of Git,
  docs and Manager memory.
- Use relevant Store, Telegram, service and PAD skills as capabilities, not
  recipes. `manager_rules.json` owns cross-system authority and integrity.

## Release

- For FST.KZ first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`, use
  `autostop-vpn-fst`, expose no secrets and never route CRM through the VPN.
- Preserve user work and remote history. Reset, rebase, force-push, restart and
  deploy need an explicit current request.
- Publish with `git push origin HEAD:AutostopManager`; release and rollback live
  in `docs/agent/deployment_runbook.md`, with a disposable Manager database.

`command_routes.json` suggests work, `knowledge_map.json` names sources, and
live registration owns schemas.
