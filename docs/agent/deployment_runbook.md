# Release and rollback

Only for an explicitly requested release. Shared authority/privacy rules:
[manager_rules.json](manager_rules.json). Preserve runtime data, credentials and
verified backups; stop on failed preflight, dirty/divergent checkouts or missing
rollback assets. Record previous Manager/work/runtime targets before activation.

## Verify and publish

`./scripts/release-gates.sh` owns local audits, tests and disposable databases.
Run on the final tree, commit that exact tree, then publish without force:

```bash
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git fetch origin AutostopManager --prune
git merge-base --is-ancestor origin/AutostopManager HEAD
git push origin HEAD:AutostopManager
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin refs/heads/AutostopManager | awk 'NR == 1 { print $1 }')"
revision="$(git rev-parse HEAD)"
```

Integrate concurrent work intentionally and rerun affected gates.

## Coupled Manager/CRM release

Read-only preflight against the persistent database must return `ok: true`:

```bash
AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3 \
  .venv/bin/python -m autostop_manager.cli store-conductor-release-gate
```

Incompatible legacy conductor state needs exact-run reconciliation before release.
`/opt/autostopcrm/deploy.sh` owns immutable Manager snapshots and coupled activation:
it replaces CRM too, including for a Manager-only revision. During failure let its
armed rollback restore assets; never manually repoint `current`. Preserve uploads
and database volumes; smoke creates no business records. Watchdog enablement is separate.

After snapshot activation:
```bash
sudo /opt/autostop-manager-releases/current/scripts/install-manager-mcp.sh --activate
```

The installer validates native transport, tool schemas, synthetic VIN and provider
failure without customer data. `--replace-unit` requires intentional replacement
of a divergent unit. Manager MCP stays loopback-only at `http://127.0.0.1:41931/mcp`,
separate from CRM/nginx; registration must match `docs/agent/manager_mcp_catalog.json`.
On initial native-endpoint failure disable only `autostop-manager-mcp.service`;
after restoring a known-good snapshot rerun its installer/probe.

If changed, install integration-audit units from the active snapshot with
`scripts/install-integration-audit-timer.sh`; verify timer enablement and finite
next elapse. Use deploy output, `integration-audit --full` and
`scripts/doctor.sh --full` only for the authorized coupled scope: these include
Store checks. Verify live Git/schema parity, public/internal smoke, required services
and readable rollback assets; container health alone is insufficient.

## Telegram-only release

Use the exact published `revision` above. Personal account:
```bash
sudo ./scripts/install-telegram-bridge.sh --account personal --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account personal "$revision"
```

Work account must be paused; `--no-start` publishes without starting it:
```bash
sudo ./scripts/set-work-telegram-duty.sh --disable
sudo ./scripts/install-telegram-bridge.sh --account work --revision "$revision"
sudo ./scripts/provision-telegram-transcription-model.sh --account work --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account work --no-start "$revision"
```

Installer/provisioner prepare revision-named candidates; deploy owns paired
source/venv/model link activation and rollback. Never switch those links manually.
Account-only releases must not restart CRM/Store or the other account.

## Work wake service

Behavior, pause and queue-loss handling live only in the
[Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md).
With work paused and its published source installed, ensure the Manager venv has
the pinned `websockets` dependency from `pyproject.toml`, then:
```bash
sudo bash /opt/autostop-work-telegram-releases/current/scripts/install-codex-wake.sh
PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-work-telegram-releases/current \
  /opt/AutostopManager/.venv/bin/python -m autostop_manager.telegram_wake probe
```

Installation keeps one task/config in root-only
`/etc/autostop-work-telegram/wake.json` outside Git; probe uses a separate
read-only synthetic task and never sends Telegram. Boot starter is oneshot and
must not stop the shared Codex daemon. Check unit validity/boot enablement,
source and Codex version parity, CRM/MCP health and the local voice route.
A reboot/customer test is separate. Enable through the skill's duty command only
after the probe passes; verify wake connected and bridge retention/status.

Wake rollback assets are `/etc/autostop-work-telegram/wake-rollback.*`.
Pause before restoring exact saved assets; preserve task/config, keep wake disabled
with old bridge code, then independently verify affected services. Unknown outcome
is not confirmed pause or restoration.
