# AutostopManager Release

Use this file for verification, publication, deployment and rollback. Publish
only source, tests, skills, catalogs and project metadata—never runtime data,
business records, identifiers, secrets, OAuth state or private output.

## Local gates

Run once on the final tree:

```bash
./scripts/release-gates.sh
```

The script keeps its database and generated files in a private disposable
directory. Warnings and missing required files fail the run.

## Publish

```bash
git status --porcelain=v1 --untracked-files=all
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git fetch origin AutostopManager --prune
git merge-base --is-ancestor origin/AutostopManager HEAD
git push origin HEAD:AutostopManager
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin refs/heads/AutostopManager | awk 'NR == 1 { print $1 }')"
```

Commit the exact reviewed tree first. Integrate concurrent work intentionally
and rerun affected checks. Never force push; the published revision must match
the remote readback.

## Coupled-release preflight

Before a coupled server update, check the persistent Manager database with the
candidate source. This command is read-only and must return `ok: true`:

```bash
AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3 \
  .venv/bin/python -m autostop_manager.cli store-conductor-release-gate
```

It blocks incompatible active legacy Store conductor state; resolve or hand off
that exact run before deployment. A planned current run is not a reason to stop.

## Activation

`/opt/autostopcrm/deploy.sh` owns coupled CRM/Manager activation and topology,
including the immutable Manager snapshot. It replaces both services even for a
Manager-only revision, so run it only with explicit authority, clean checkouts,
usable rollback assets and backup evidence. Preserve `.env`, uploads and
PostgreSQL volumes; smoke must not create an order or supplier purchase. Enable
`AUTOSTOP_INSTALL_WATCHDOG=1` only when requested.

If integration-audit units changed, run
`sudo /opt/autostop-manager-releases/current/scripts/install-integration-audit-timer.sh`;
verify the enabled timer and a finite next elapse rather than expecting its
oneshot service to stay active.

## Native Manager MCP endpoint

The native Manager surface is a loopback-only service at
`http://127.0.0.1:41931/mcp`. It is intentionally separate from the public CRM
Gateway: CRM keeps its own published CRM catalog and must not advertise Manager
tools that it does not route.

After the immutable Manager snapshot is active, install or update the unit from
that exact snapshot and run its safe transport probe:

```bash
sudo /opt/autostop-manager-releases/current/scripts/install-manager-mcp.sh --activate
sudo systemctl status --no-pager autostop-manager-mcp.service
```

The installer refuses to overwrite a divergent installed unit unless
`--replace-unit` is supplied deliberately. Its activation probe validates native
`ping`, `tools/list`, catalog status, category search, a synthetic dry-run VIN
resolver, and a structured forced provider failure. It sends no customer VIN,
token, correspondence, order, price, reserve, or write request. Repeat the
same probe manually when diagnosing an already active service:

```bash
PYTHONPATH=/opt/autostop-manager-releases/current \
  PYTHONSAFEPATH=1 \
  AUTOSTOP_MANAGER_ENV_FILE=/dev/null \
  AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3 \
  /opt/AutostopManager/.venv/bin/python -m autostop_manager.cli mcp-probe \
    --url http://127.0.0.1:41931/mcp --provider-failure-check
```

Do not add this endpoint to nginx or the CRM Gateway. Codex connects to the
loopback Manager endpoint through its separate `autostopmanager` MCP entry;
the active Manager `tools/list` must match
`docs/agent/manager_mcp_catalog.json`.

For an initial endpoint failure, stop and disable only the native service; this
does not alter CRM, Store, network, or business data:

```bash
sudo systemctl disable --now autostop-manager-mcp.service
```

For a failure while `/opt/autostopcrm/deploy.sh` is still running, let its
armed rollback restore the previous immutable Manager snapshot and CRM state;
do not manually repoint `current`. After a source rollback or restoration of a
known-good Manager snapshot, run the installer with `--activate` again and
repeat the transport probe before reopening the endpoint.

## Readback

Use the CRM deploy output as its Gateway, connector and OAuth evidence. From
Manager run `integration-audit --full` and `scripts/doctor.sh --full`; verify the
public-camera sandbox separately when that component changed.

Completion needs matching live schemas/manifests, healthy required services,
public and internal smoke, clean audits, the active GitHub revision and readable
rollback refs. Container health alone is not enough.

## Telegram-only release

After normal gates and explicit authority, deploy only the selected isolated
account. For the personal account, the scripts verify the clean checkout and
exact published revision:

```bash
git fetch origin AutostopManager --prune
revision="$(git rev-parse origin/AutostopManager)"
sudo ./scripts/install-telegram-bridge.sh --account personal --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account personal "$revision"
```

The installer only prepares dependencies; deploy atomically publishes the unit
and immutable release and restores prior account assets on activation failure.
Neither changes CRM, Store, VPN, nginx, another account or the working tree.

Update work dependencies and its transcription model only while duty is paused.
The installer and provisioner build a revision-named candidate under
`/opt/autostop-work-telegram-runtimes/<revision>`; they do not replace the
stable venv/model paths. During the paused deploy, both stable links move to the
candidate together with the source release. If activation or the local voice
check fails, deploy restores the previous source, unit, wrappers and both
runtime links. The first transition moves a legacy direct runtime under the
same runtime root while duty is paused.

Work-only `--no-start` is required for a work release. It validates and
publishes the local voice route without enabling or starting the service.

```bash
sudo ./scripts/set-work-telegram-duty.sh --disable
sudo ./scripts/install-telegram-bridge.sh --account work --revision "$revision"
sudo ./scripts/provision-telegram-transcription-model.sh --account work --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account work --no-start "$revision"
```

For the event-triggered work mode, continue with the wake installation below
before enabling Telegram. The duty command is its lifecycle interface; do not
replace it with a raw restart.

## Event-triggered work Telegram and Codex

The wake service receives opaque refs from the work bridge uid only; root owns
control/configuration. It uses the existing Codex Unix WebSocket directly, with
compression disabled for 0.153.4 (the proxy is a byte tunnel, not JSON-lines).
Behavior and loss limits have one source: the Telegram skill linked below.
After gates, publication, Manager/CRM/MCP activation and paused work deployment,
install the pinned `websockets` dependency in the Manager venv, then:

```bash
sudo /opt/autostop-work-telegram-releases/current/scripts/install-codex-wake.sh
PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-work-telegram-releases/current \
  /opt/AutostopManager/.venv/bin/python -m autostop_manager.telegram_wake probe
```

Setup creates one task with existing model/auth/tools and a root-only 0600
`/etc/autostop-work-telegram/wake.json`, outside Git. Probe verifies started,
exact synthetic output and completed, then archives its separate read-only task;
it never sends Telegram or changes business data.
`autostop-codex-start.service` is a oneshot boot starter; it never stops the shared
daemon. It handles the existing unmanaged daemon that rejects `daemon bootstrap`.
Verify both units with `systemd-analyze verify` and confirm boot enablement.
A real server reboot is a separate test. Activation and content-free readback:

```bash
sudo ./scripts/set-work-telegram-duty.sh --enable
sudo ./scripts/set-work-telegram-duty.sh --status
sudo -u autostop-work-telegram env \
  PYTHONPATH=/opt/autostop-work-telegram-releases/current \
  /opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge \
    --account work monitor-status
```

Wake status must report `enabled: true`, `connected: true`, `polling: false`.
Bridge status must report `enabled: true` and `retention: memory_only`.
Repeat `--enable`: the bridge PID, monitor epoch and wake queue must not change.
`--disable` interrupts the automatic turn before stopping wake/bridge; an unknown
outcome is an error, never a confirmed pause. Failure/overflow is visible in status.
Do not manually run overlapping turns in the dedicated automation task.
Before enabling, verify published/installed source parity, Codex version parity,
CRM/MCP health, voice tools and the server probe. No customer test is implied.

`open_events` counts unresolved refs; `retained_events` is the bounded buffer,
not completed work. Check `dropped_open_events` and `started_at` for overflow
or an epoch change before assessing continuity. Old event refs cannot identify
messages in a new daemon epoch. The monitor stores neither content nor contact
entities; only the technical Telegram update cursor may persist. For bounded
intake and reply semantics see the
[Telegram skill](../../.agents/skills/manage-owner-telegram/SKILL.md).

## Failure and rollback

Record previous Manager/work/runtime targets and verified database backups.
Wake unit/config backups are root-only `/etc/autostop-work-telegram/wake-rollback.*`.
Pause before restoring exact assets; retain the task/config for reuse. Keep wake
disabled with old bridge code. Business data and unrelated files are not cleanup targets.

Stop on unmatched checkouts, failed backup, schema drift, missing rollback proof
or unhealthy preflight. Use the deploy script's rollback assets, then reread the
affected endpoint and service checks before claiming restoration. Do not point a
stable work runtime link at a candidate manually: deployment owns the paired
source/runtime switch and rollback.
