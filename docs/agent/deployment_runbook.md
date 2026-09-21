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
`store-conductor-release-gate` against persistent Store state; reconcile blocked
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
components before enabling work. [Refresh/test Codex](../mcp_release_checks.md).

## Automation Center release state

Automation Center has a separate root-only registry at
`/var/lib/autostop-manager-scheduler/registry.sqlite3`; it is never placed in
the CRM-mounted Manager data directory. Generate a unique, non-reused release
attempt key and first run `python -m autostop_manager.automation_release hold
--release-attempt-key ATTEMPT`; require its machine-readable `quiescent=true`
and held-state readback. A revision/SHA alone is not a release attempt key.
Create an online backup before any installer or schema migration, to a new file
in a pre-created root-owned `0700` backup directory with
`scripts/backup-manager-automation-state.py --output ABSOLUTE_NEW_PATH`; the
helper uses SQLite backup, verifies the component schema and writes atomically
with mode `0600`.

Before the first scheduler start, adopt the allowlisted system timers under the
same owned hold with `python -m autostop_manager.automation_release adopt-current
--release-attempt-key ATTEMPT`; require `timers_verified=true`. Adoption is
create-if-absent and never overwrites persisted desired state.

The coordinating deploy must also snapshot the exact scheduler unit, all three
managed timer drop-ins (including absence), and the work-Telegram duty config;
the SQLite helper does not cover those files. Install from the active immutable
Manager release with `scripts/install-manager-automation.sh
--activate-under-hold --release-attempt-key ATTEMPT --manager-revision SHA
--crm-revision CRM_SHA [--crm-version CRM_VERSION]`;
the revision must come from the sealed release manifest or deploy input because
an immutable archive need not contain `.git`. The CRM revision is required for
activation and is recorded only as bounded technical metadata in the readiness
packet; do not copy CRM secrets into this config. Replacing a changed installed unit
requires `--replace-unit`. The installer starts under hold, adopts timers only
when absent, and verifies/seeds `crm_digest_v1` strictly OFF. After activation, verify
`autostop-manager-scheduler.service`, the root:10001 `0750` runtime directory,
the `0660` control socket, readiness, templates, jobs and all five adopted
timer states. Release only after the coordinated CRM/Telegram smoke checks with
`python -m autostop_manager.automation_release release-hold
--release-attempt-key ATTEMPT`. A code
rollback must preserve the registry; restore a backup only for an explicit
state-recovery decision, never merely because code activation failed.
