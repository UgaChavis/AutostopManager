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

After deploy, compare installed Manager/CRM revisions with the published inputs;
run `.venv/bin/python -m autostop_manager.cli doctor --integrations --full`, verify
the work-Telegram duty state was restored, and require old integration-audit and
watchdog units to be absent. Before live research, require `autostop-j1.service`
active and run
`env PYTHONPATH=/opt/autostop-manager-releases/current
AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1
AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890
/usr/bin/python3 -m autostop_manager.j1_research probe`. The seven-day cache never
becomes Manager customer memory. Then [refresh and test Codex MCP](../mcp_release_checks.md).

## Recovery or standalone component work

Use these paths only outside the normal coordinated deploy, with one exact
published revision and a preserved rollback state. For work Telegram, pause duty,
run `scripts/install-telegram-bridge.sh --account work --revision <commit>`,
`scripts/provision-telegram-transcription-model.sh --account work --revision <commit>`,
then `scripts/deploy_telegram_bridge.sh --account work --no-start <commit>` and
the active snapshot's `scripts/install-codex-wake.sh`; do not switch links manually.
Probe CRM/MCP, voice, systemd and versions without client sends before enabling
through the [Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md).
Personal Telegram is a separate release; preserve its session.

Automation Center state lives in the root-only
`/var/lib/autostop-manager-scheduler/registry.sqlite3`. A standalone repair must
use one unique release-attempt key with `python -m autostop_manager.automation_release
hold --release-attempt-key ATTEMPT` and require `quiescent=true`. Make an online
registry backup with `scripts/backup-manager-automation-state.py --output NEW_PATH`
in a root-owned `0700` directory; separately snapshot the scheduler unit, managed
timer drop-ins and Telegram duty config. Install only from the active immutable
release under that hold with `scripts/install-manager-automation.sh
--activate-under-hold --release-attempt-key ATTEMPT --manager-revision SHA
--crm-revision CRM_SHA [--crm-version CRM_VERSION]`. Release the hold only after
scheduler, socket, readiness, all five timer states and coordinated CRM/Telegram
smoke checks pass, using `python -m autostop_manager.automation_release release-hold
--release-attempt-key ATTEMPT`.

On failure keep work paused and verify the restored components before enabling it.
Preserve volumes, uploads and the Automation registry; never overwrite newer
business operations with an old database. Restore the registry backup only for an
explicit state-recovery decision, never merely because code activation failed.
