# AutostopManager Release

This file owns verification, GitHub publication, production deploy and rollback.
Publish only source, tests, skills, catalogs and project metadata—never runtime
data, business records, identifiers, secrets, OAuth state or private output.

## Local gates

Run once on the final tree with a disposable Manager database:

```bash
run_manager_release_gates() (
  set -e
  local gate_dir
  gate_dir="$(mktemp -d /tmp/autostop-manager-release-gates.XXXXXX)"
  trap '[[ "$gate_dir" == /tmp/autostop-manager-release-gates.* ]] && rm -rf -- "$gate_dir"' EXIT
  export AUTOSTOP_MANAGER_DB="$gate_dir/preflight.sqlite3"
  .venv/bin/python -m autostop_manager.cli knowledge-sync
  .venv/bin/python -m autostop_manager.cli knowledge-audit
  .venv/bin/python -m autostop_manager.cli skills-audit
  .venv/bin/python -m autostop_manager.cli cleanup-audit
  .venv/bin/python -m ruff check .
  .venv/bin/python -m ruff format --check autostop_manager tests
  .venv/bin/python -m mypy autostop_manager
  .venv/bin/python -m coverage run -m pytest -q
  .venv/bin/python -m coverage report --fail-under=82
  git diff --check
)
run_manager_release_gates
```

Required warnings and missing files must be empty. Preserve existing work and
stage only intended files; never point these gates at the persistent database.

## Publish

```bash
git fetch origin AutostopManager --prune
git merge-base --is-ancestor origin/AutostopManager HEAD
git push origin HEAD:AutostopManager
git rev-parse HEAD
git ls-remote origin refs/heads/AutostopManager
```

Integrate concurrent work intentionally and rerun affected checks. Never force
push; local and remote revisions must match.

## Server preflight

Before a coupled server update, check the persistent Manager database with the
candidate source. This command is read-only and must return `ok: true`:

```bash
AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3 \
  .venv/bin/python -m autostop_manager.cli store-conductor-release-gate
```

It blocks active legacy Store conductor state; resolve that exact business run
or hand it off before deployment. Also verify AutoCRM uses the current Manager
Store-conductor schema as one release unit.

## Production

- Manager source is `/opt/AutostopManager`; immutable releases live in
  `/opt/autostop-manager-releases/` and `current` selects the active snapshot.
- CRM source is `/opt/autostopcrm`; its API/MCP listen on `127.0.0.1:8000/8001`
  and public MCP is `https://crm.autostopcrm.ru/mcp`.
- Store source is `/opt/autostop-app`; the public site is
  `https://autostop24.shop`.
- CRM imports Manager tools from the immutable release. CRM and Store share the
  dedicated internal agent network, not a database.

`/opt/autostopcrm/deploy.sh` replaces both CRM and Manager even for a
Manager-only revision. Run it only with explicit authority to advance/restart
CRM, matching clean checkouts and usable rollback assets. Preserve `.env`,
uploads and PostgreSQL volumes; require backup evidence and internal/public
smoke. Smoke must not create an order or supplier purchase. Enable
`AUTOSTOP_INSTALL_WATCHDOG=1` only when that installation was requested.

If the integration-audit units changed, install them from the active immutable
release and verify the service plus a finite enabled timer.

## Readback

After activation, run the exhaustive Gateway and live-connector checks from CRM
(the OAuth check creates and revokes test state). From Manager run
`control-report`, `integration-audit --full` and `scripts/doctor.sh --full`, and
verify the public-camera sandbox separately.

Completion requires live schemas/manifests matching the release, healthy
CRM/Gateway/nginx and required units, public and internal smoke, clean audits,
Store-aware degradation, the GitHub revision active, and readable rollback
refs. Container health alone is not enough.

## Telegram-only release

After normal gates, explicit authority and an exact published revision, deploy
only the selected isolated account:

```bash
git fetch origin AutostopManager --prune
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git branch --show-current)" = AutostopManager
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/AutostopManager)"
revision="$(git rev-parse origin/AutostopManager)"
test "$revision" = "$(git ls-remote origin refs/heads/AutostopManager | awk 'NR == 1 { print $1 }')"
sudo ./scripts/install-telegram-bridge.sh --account work --revision "$revision"
sudo ./scripts/provision-telegram-transcription-model.sh --account work --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account work "$revision"
```

These scripts use an immutable account release and roll back that account on
failure. They must not change CRM, Store, VPN, nginx, another account or the
working tree. Store and Telegram remain separate surfaces; a Telegram-only
release never changes Store or CRM.

## Failure and rollback

Stop on unmatched checkouts, failed backup, schema drift, missing rollback proof
or unhealthy preflight. Use the deploy script's exact rollback assets, then
repeat the affected endpoint, container and Gateway checks before claiming
restoration.
