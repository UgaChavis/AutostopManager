# Release and rollback

[Boundaries](../../AGENTS.md). Test in a separate worktree; preserve active sources.
CRM keeps its Python registrar and guarded ledger; native MCP omits them.

## Verify and publish

Run `./scripts/release-gates.sh` with disposable data; coverage stays >=82%.
Commit, fetch `origin/AutostopManager`, merge without force and rerun gates.
Push `git push origin HEAD:AutostopManager`;
compare HEAD with `git ls-remote origin refs/heads/AutostopManager`; require green CI.

## Future authorized server migration

Back up releases, configuration and databases. Pause Telegram; verify no active turn.
Before activating Manager run
`scripts/remove-learning-hooks.py --apply` with Python 3.11+: it preserves unrelated
hooks and prints a backup path. Keep old hook files/database for rollback.
`--restore BACKUP --apply` requires an unchanged replacement configuration.

`/opt/autostopcrm/deploy.sh` also restarts CRM. First run the CLI
`store-conductor-release-gate` against persistent Store state; reconcile blocked
legacy runs. Its `knowledge-sync`/`knowledge-audit` calls now only check documents.

Deploy owns activation/rollback; never repoint `current`. Manager's private `.env`
needs `AUTOSTOP_STORE_API_URL` (host loopback) and Store READ/MANAGE/QUOTE/OWNER tokens;
CRM's environment is separate. Run the active `scripts/install-manager-mcp.sh --activate`.
`doctor --integrations --full` checks CRM, native Manager/Store and Gmail.
Retire the integration-audit timer explicitly; old watchdog units must be absent.

## Telegram and rollback

Preserve the work task and private wake configuration. With work paused, use
the published revision with `install-telegram-bridge.sh --account work --revision`,
`provision-telegram-transcription-model.sh --account work --revision`, then
`deploy_telegram_bridge.sh --account work --no-start REVISION`. Install wake via
the active Telegram snapshot's `scripts/install-codex-wake.sh`. These installers
own paired source/venv/model activation; do not switch links manually.

Run `python -m autostop_manager.telegram_wake probe` with active Telegram PYTHONPATH;
No client sends. Verify CRM/MCP, voice, systemd and versions. Enable through the
[Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md) only after
checks pass. Personal Telegram is a separate account release; preserve its session.

On failure keep work paused, roll back the release and reinstall its MCP endpoint.
Restore learning hooks only with the old Manager available. Preserve volumes/uploads;
never overwrite new business operations with an old database. Verify restored
components before enabling work. Reconnect Codex tools; test in a new task.
