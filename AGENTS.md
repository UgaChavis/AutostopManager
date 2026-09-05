# AutostopManager Codex Instructions

## Work

- Answer the owner concisely in Russian. With customers, infer the likely goal
  from available context and ask only for a fact that can change the next useful
  action.
- For a non-trivial task, `agent-brief` can quickly suggest routes and sources.
  Combine, adapt or ignore them when the evidence points elsewhere;
  `knowledge-probe` only finds sources.
- Default to `work`; use `$autostop-learning-loop` only when learning is asked
  for. Keep resumable state as compact refs, hashes and versions.

## Systems

- CRM, AutoStop App, Gmail and Telegram own their live data. Read focused,
  current context from the configured surface; keep raw business data, personal
  identifiers and secrets out of Git, docs and Manager memory.
- Use `$manage-autostop-store`, `$manage-owner-telegram`,
  `$resolve-autostop-service-case` and `$remote-diagnostics-pad-vii` when useful,
  without treating a skill as a script. `manager_rules.json` is the single owner
  of cross-system authority and data-integrity boundaries.

## Release

- For FST.KZ first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`, use
  `autostop-vpn-fst`, expose no secrets and never route CRM through the VPN.
- Preserve user work and remote history. Reset, rebase, force-push, restart and
  deploy only when the current request explicitly includes them.
- Publish with `git push origin HEAD:AutostopManager`; follow
  `docs/agent/deployment_runbook.md` for release or rollback and use a disposable
  Manager database for release gates.

`command_routes.json` recognizes work, `knowledge_map.json` names each source,
`manager_rules.json` owns global invariants, and live registration owns schemas.
