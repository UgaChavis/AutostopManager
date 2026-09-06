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

## Failure and rollback

Stop on unmatched checkouts, failed backup, schema drift, missing rollback proof
or unhealthy preflight. Use the deploy script's rollback assets, then reread the
affected endpoint and service checks before claiming restoration.
