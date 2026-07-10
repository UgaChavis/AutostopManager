# AutoStop Manager Deployment Runbook

This is the canonical publish, deploy, smoke, and rollback procedure. AutoStop
Manager is mounted into the CRM container; production does not run a separate
Manager `systemd` service.

## Safety boundary

Publish code, tests, tracked docs/catalogs, project metadata, the dependency
lock, and CI configuration. Never publish `data/`, `.env*`, SQLite databases,
CRM/Gmail exports, credentials, private topology, generated business files,
logs, browser profiles, or test caches.

The production checkout is `/opt/AutostopManager`; the CRM checkout and compose
project are `/opt/autostopcrm`. Resolve remote/branch names from Git at runtime
instead of embedding credentials or private SSH key paths in this document.

## Preflight

1. Record the exact local and remote commits without printing secrets:

   ```bash
   cd /opt/AutostopManager
   git status --short --branch
   git rev-parse HEAD
   git branch --show-current
   git remote -v
   git fetch --prune origin
   ```

2. Confirm disk, container, and mount state:

   ```bash
   df -h / /opt
   docker compose -f /opt/autostopcrm/docker-compose.yml ps
   docker inspect autostopcrm --format '{{range .Mounts}}{{println .Source "->" .Destination .Mode}}{{end}}'
   ```

3. Require a clean, committed candidate and a remote branch at the same commit.
   Do not deploy an uncommitted tree and do not use force-push or history
   rewriting.

4. Run every gate in `docs/agent/development.md`. Additionally run:

   ```bash
   bash scripts/doctor.sh --full
   git status --short --ignored
   ```

## Backup and rollback point

Create a root-only timestamped directory outside the checkout. Record the
pre-deploy commit, create a consistent SQLite backup, and save only the minimum
runtime configuration needed for recovery:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/autostopmanager-backups/deploy-$stamp"
install -d -m 700 "$backup"
git -C /opt/AutostopManager rev-parse HEAD > "$backup/commit.txt"
AUTOSTOP_BACKUP="$backup" /opt/AutostopManager/.venv/bin/python - <<'PY'
import os, sqlite3
from pathlib import Path
from autostop_manager.config import get_db_path

source = get_db_path()
target = Path(os.environ["AUTOSTOP_BACKUP"]) / "manager.sqlite3"
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
target.chmod(0o600)
PY
sha256sum "$backup/commit.txt" "$backup/manager.sqlite3" > "$backup/SHA256SUMS"
```

If an environment/config backup is required, copy it directly into that mode
`0700` directory and never print or add it to Git. Verify SQLite with
`PRAGMA integrity_check` before restarting anything.

Rollback is: restore the recorded commit with a non-destructive branch switch
or fast-forwardable revert, restore the consistent SQLite backup only if a
schema/data rollback is required, restart the CRM container, then rerun all
smokes. Never use `git reset --hard` against an unreviewed working tree.

## Dependency and migration step

Local standalone Manager environment:

```bash
cd /opt/AutostopManager
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -c 'from autostop_manager.storage import ManagerMemoryStore; ManagerMemoryStore().initialize()'
```

`initialize()` runs versioned, restart-safe SQLite migrations. Verify
`PRAGMA user_version`, the migration ledger, and `PRAGMA integrity_check` after
it completes. The CRM container owns its Python environment; do not install
packages interactively inside it. Rebuild the CRM image only when its declared
dependency set changes.

## Restart

Manager source-only changes require a restart of the CRM service that imports
the mounted package; they do not require a host reboot or a new service:

```bash
cd /opt/autostopcrm
docker compose restart autostopcrm
docker compose ps autostopcrm
```

Use the CRM deploy script only when the CRM image/configuration also changed.
Do not pass passwords on the command line; smoke credentials must be supplied
through protected environment variables read by the CRM checker.

## Required smoke checks

All of these must be observed, not inferred:

1. Package and MCP registry inside the running container:

   ```bash
   docker compose -f /opt/autostopcrm/docker-compose.yml exec -T \
     --workdir /opt/AutostopManager autostopcrm \
     python -c 'import asyncio, autostop_manager; from autostop_manager.mcp_server import build_server; print(len(asyncio.run(build_server().list_tools())))'
   ```

2. CLI import, semantic routing, knowledge/docs/contracts, SQLite read, and a
   temporary-store write/readback test outside production data.
3. Local and public MCP `initialize`, followed by `tools/list`; confirm the
   expected Manager tool count and representative schemas.
4. Read-only CRM connector health and board-context access. Do not print card,
   client, vehicle, order, payment, or cashbox contents.
5. Gmail connector readiness/auth metadata only; do not read or mutate message
   content for a deployment smoke.
6. Container health, recent logs, and host checks:

   ```bash
   bash /opt/AutostopManager/scripts/doctor.sh
   docker compose -f /opt/autostopcrm/docker-compose.yml logs --since=10m --tail=200 autostopcrm
   ```

   Search the bounded log output for exceptions and secret-like values using
   redacted tooling; do not paste raw logs into a report.

7. Confirm all three identities match the deployed commit: local `HEAD`, the
   remote branch SHA, and the commit recorded in the deployment report.

If any smoke fails, diagnose and retry one safe fix. If the fix would expand
scope or risk business data, perform the documented rollback and report the
exact failing check.
