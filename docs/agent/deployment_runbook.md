# Release and rollback

[Boundaries](../../AGENTS.md). Test in a separate worktree; preserve active sources.
CRM keeps its Python registrar and guarded ledger; native MCP omits them.

## Verify and publish

Run `./scripts/release-gates.sh` with disposable data; coverage stays >=82%.
Merge `origin/AutostopManager` into the feature branch, rerun gates, push and
merge its green PR. Confirm the published revision with `git ls-remote`.

## Authorized coordinated release

First run `.venv/bin/python -m autostop_manager.cli store-conductor-release-gate`
against persistent Store state and reconcile blocked legacy runs. Run
`scripts/remove-learning-hooks.py` in dry-run; use `--apply` only when it reports
a required legacy-hook migration, and keep its backup for rollback.

The only normal-path orchestrator is `/opt/autostopcrm/deploy.sh`; it restarts CRM
and owns release/config/database backups, the Automation Center hold, work-Telegram
pause and activation, immutable Manager activation, verification and rollback.
Do not pre-acquire a separate hold, run component installers around it, or repoint
any `current` link manually. Manager's private `.env` needs the loopback
`AUTOSTOP_STORE_API_URL` and Store READ/MANAGE/QUOTE/OWNER tokens; CRM's environment
is separate.

Before deploy, switch the clean server checkout to `AutostopManager`, fetch and
fast-forward it to the published revision. `deploy.sh` runs the pinned
[catalog sync](../offline_parts_catalogs.md) before maintenance and activates
native Manager MCP by default. For static J1, set `AUTOSTOP_J1_ACTIVATE_ON_DEPLOY=1`.
Browser rendering remains opt-in through `AUTOSTOP_J1_BROWSER_ACTIVATE_ON_DEPLOY=1`
and only after the deploy's 2 GiB `MemAvailable`, 1 GiB `SwapFree` and 60-second
zero-swap-I/O preflight. Never create the browser attestation marker by hand.

After deploy, verify installed Manager/CRM revisions, bounded MCP/CRM/Store
probes, `doctor --integrations` (no `--full`), work-Telegram duty restoration,
and absence of old integration-audit/watchdog units. Full doctor creates a test
workflow; run it only with disposable CRM/Store. Before live research, require `autostop-j1.service`
active and run
`env PYTHONPATH=/opt/autostop-manager-releases/current
AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1
AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890
/usr/bin/python3 -m autostop_manager.j1_research probe`. The seven-day cache never
becomes Manager customer memory. Then [refresh and test Codex MCP](../mcp_release_checks.md).

## Recovery or standalone component work

Outside coordinated deploy, use one published revision and preserved rollback.
For work Telegram, pause duty, follow the ordered [bridge/wake procedure](module_operations/telegram_automation.md)
including `scripts/install-codex-wake.sh`,
probe CRM/MCP, voice, systemd and versions without client sends, then enable
through the [Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md).
Personal Telegram is a separate release; preserve its session.

For standalone Automation repair, use the [hold/backup/install procedure](module_operations/telegram_automation.md)
with one release-attempt key. Require a quiescent hold, online registry backup in
a root-owned `0700` directory, and snapshots of unit, timer drop-ins and duty.
Release the hold only after scheduler, socket, readiness, five timers and
CRM/Telegram smoke pass. On failure keep work paused and verify restored components.
Preserve volumes, uploads and registry; restore an old registry only after an
explicit state-recovery decision, never over newer business operations.
