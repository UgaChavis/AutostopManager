# AutostopManager Codex Instructions

- Answer the owner concisely in Russian. Solve for the outcome and evidence; ask
  only when the answer changes the next useful move.
- `agent-brief`, `knowledge-probe`, routes and skills are optional starting
  points. Choose, combine, replace or invent steps; explore safely when unsure.
- Work by default. Learn only in effective `learning` mode or at owner request.
- CRM, AutoStop App, Gmail and Telegram own live data. Read the minimum; keep
  business data, correspondence, identifiers and secrets out of Manager memory,
  Git and docs.
- `docs/agent/manager_rules.json` contains the hard boundaries; routes never
  authorize effects. Load only needed sources from `knowledge_map.json`; live
  registration owns schemas and execution gates.
- Preserve user work and remote history. Reset, rebase, force-push, live restart
  and deploy require an explicit current request.
- For FST.KZ first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`, use
  `autostop-vpn-fst`, expose no secrets and never route CRM through the VPN.
- Publish with `git push origin HEAD:AutostopManager`; release and rollback use
  `docs/agent/deployment_runbook.md` and a disposable Manager database.
