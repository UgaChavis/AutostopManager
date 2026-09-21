# Release and rollback

[Boundaries](../../AGENTS.md). Test in a separate worktree; preserve active sources.
CRM keeps its Python registrar and guarded ledger; native MCP omits them.

## Verify and publish

Run `./scripts/release-gates.sh` with disposable data; coverage stays >=82%.
Commit, fetch `origin/AutostopManager`, merge without force and rerun gates.
Push `git push origin HEAD:AutostopManager`;
compare HEAD with `git ls-remote origin refs/heads/AutostopManager`; require green CI.

## Authorized server migration

Back up releases, configuration and databases. Pause Telegram; verify no active turn.
Before activating Manager run
`scripts/remove-learning-hooks.py --apply` with Python 3.11+: it preserves unrelated
hooks and prints a backup path. Keep old hook files/database for rollback.
`--restore BACKUP --apply` requires an unchanged replacement configuration.

`/opt/autostopcrm/deploy.sh` also restarts CRM. First run the CLI
`.venv/bin/python -m autostop_manager.cli store-conductor-release-gate` against persistent Store state; reconcile blocked
legacy runs. Its `knowledge-sync`/`knowledge-audit` calls now only check documents.

Deploy owns activation/rollback; never repoint `current`. Manager's private `.env`
needs `AUTOSTOP_STORE_API_URL` (host loopback) and Store READ/MANAGE/QUOTE/OWNER tokens;
CRM's environment is separate. Run the active `scripts/install-manager-mcp.sh --activate`.
For the first J1 release, invoke the coordinated deploy with
`AUTOSTOP_MANAGER_MCP_ACTIVATE_ON_DEPLOY=1 AUTOSTOP_J1_ACTIVATE_ON_DEPLOY=1`.
The deploy installs and probes the cache-scoped `autostop-j1.service` from the
immutable Manager snapshot after candidate CRM checks. It restores the previous
unit and service state on rollback. J1 keeps only a seven-day temporary research
corpus under `/var/cache/autostop-j1`; inspect `systemctl is-active autostop-j1`
and run `env PYTHONPATH=/opt/autostop-manager-releases/current
AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1
AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890
/usr/bin/python3 -m autostop_manager.j1_research probe` before a live
research job. Never move this cache into Manager's persistent customer memory.
Browser rendering remains opt-in: add `AUTOSTOP_J1_BROWSER_ACTIVATE_ON_DEPLOY=1`
only when the host has at least 2 GiB `MemAvailable`, 1 GiB `SwapFree`, and no
swap I/O for the immediately preceding 60 seconds. The deploy snapshots only
the browser unit/attestation state, starts the isolated two-container stack and
runs its immutable-release verifier. A failed verifier rolls back that browser
state; static J1 remains available. Do not create an attestation marker by hand.
`.venv/bin/python -m autostop_manager.cli doctor --integrations --full` checks CRM,
native Manager/Store and Gmail.
Retire the integration-audit timer explicitly; old watchdog units must be absent.

## Telegram and rollback

Preserve the work task and private wake configuration. With work paused, use
the published revision with
`scripts/install-telegram-bridge.sh --account work --revision <commit>`,
`scripts/provision-telegram-transcription-model.sh --account work --revision <commit>`,
then `scripts/deploy_telegram_bridge.sh --account work --no-start <commit>`. Install wake via
the active Telegram snapshot's `scripts/install-codex-wake.sh`. These installers
own paired source/venv/model activation; do not switch links manually.

Run `python -m autostop_manager.telegram_wake probe` with active Telegram PYTHONPATH;
No client sends. Verify CRM/MCP, voice, systemd and versions. Enable through the
[Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md) only after
checks pass. Personal Telegram is a separate account release; preserve its session.

On failure keep work paused, roll back the release and reinstall its MCP endpoint.
Restore learning hooks only with the old Manager available. Preserve volumes/uploads;
never overwrite new business operations with an old database. Verify restored
components before enabling work. [Refresh/test Codex](../mcp_release_checks.md).
