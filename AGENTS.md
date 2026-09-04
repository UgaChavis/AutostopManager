# AutostopManager Codex Instructions

Compact startup contract for `/opt/AutostopManager`; task playbooks own the details.

## Start

- Answer the owner in concise, direct Russian. In voice and customer dialogue,
  speak naturally and ask only questions that materially help the next action.
- For a non-trivial task run once:
  `.venv/bin/python -m autostop_manager.cli agent-brief "<query>"`. Use the
  returned route and the project venv; `knowledge-probe` finds documents but
  grants no write, connector or financial authority.
- Default to `work`. If the owner asks for learning, use
  `autostop-learning-loop`. Keep multi-step state in the Gateway v2 ledger as
  compact refs, hashes and versions.

## Sources And Privacy

- CRM owns service records and board state. AutoStop App owns Store catalog,
  stock, estimates and orders. Gmail and Telegram own their messages and media.
- Manager keeps routes, cross-system rules, technical cursors and approved,
  de-identified lessons—not copies of live business data.
- Never persist raw exports, correspondence, customer or vehicle identifiers,
  money ledgers, credentials, OAuth state or secrets in Git, docs, memory or
  workflow state. Inspect only explicit non-secret configuration fields; never
  dump `.env`, Docker environments or process environments.

## Live Work

- The configured CRM Gateway v2 is the external project surface. Begin with
  `agent_bootstrap`, then use `agent_board_digest`, focused search and exact
  entity context. Internal Manager tools and unconfigured App namespaces are
  not alternative live surfaces.
- A direct request authorizes the ordinary, reversible steps needed for its
  result—not unrelated cleanup, recipients, purchases or deployment. Choose the
  shortest useful path and adapt it to the evidence instead of following a
  script mechanically.
- Route Store work through `$manage-autostop-store`; its Admin V2 conductor owns
  customer quotes. Route PAD VII work through `remote_diagnostics_pad_vii` and
  require the owner's explicit live session plus fresh observation.
- Prefer named workflows. Raw capabilities are read-only by default; any rare
  raw mutation follows the exact exception in `manager_rules.json`.
- Before a mutation, reread the exact target, prepare its action contract,
  dry-run, apply idempotently with concurrency protection, then reread the
  result. An unknown outcome stays unresolved until reconciled.
- Payments, cashboxes, refunds, payroll, supplier orders, financial totals,
  destructive changes and a new external recipient require exact authority.
  Keep private messages transient and verify the account, peer and delivery.

## Release Boundary

- For FST.KZ first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`, use
  `autostop-vpn-fst`, expose no secrets and never route CRM through the VPN.
- Preserve dirty work. Never reset, rebase, force-push, remove a release,
  restart or deploy unless the current request says so.
- Publish Manager with `git push origin HEAD:AutostopManager`. Deployment and
  rollback live only in `docs/agent/deployment_runbook.md`; release preparation
  does not authorize deployment. Its isolated gates must never sync knowledge
  into the persistent Manager database.
- Keep one owner for each rule and link to it rather than copying it.

Navigation: `command_routes.json` selects workflows, `knowledge_map.json`
selects documents, `manager_rules.json` holds cross-system invariants, and live
Manager/CRM registrations define tool schemas.
