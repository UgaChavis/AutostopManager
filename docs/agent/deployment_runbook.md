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
account. The scripts verify the clean checkout and exact published revision:

```bash
git fetch origin AutostopManager --prune
revision="$(git rev-parse origin/AutostopManager)"
sudo ./scripts/install-telegram-bridge.sh --account work --revision "$revision"
sudo ./scripts/provision-telegram-transcription-model.sh --account work --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account work "$revision"
```

They use an immutable account release and roll back that account on failure.
They do not change CRM, Store, VPN, nginx, another account or the working tree.

For an explicitly authorized work-Telegram inbound-monitor release, verify the
same account after deployment without exposing dialogue content:

```bash
sudo -u autostop-work-telegram env \
  PYTHONPATH=/opt/autostop-work-telegram-releases/current \
  /opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge \
    --account work monitor-status
```

The result must report `enabled: true` and `retention: memory_only`. A test
message may then be checked with `monitor-events`; inspect a single opaque event
only when the owner explicitly requests it. The monitor keeps only a private
chat/message reference, so a first message from an unknown contact does not
require resolving or persisting a Telegram entity. It must not send, acknowledge,
download, or persist conversation content or Telegram entity records.
Telegram may retain its technical update cursor and existing session authorization;
neither is a dialogue or contact journal.

## Failure and rollback

Stop on unmatched checkouts, failed backup, schema drift, missing rollback proof
or unhealthy preflight. Use the deploy script's rollback assets, then reread the
affected endpoint and service checks before claiming restoration.
