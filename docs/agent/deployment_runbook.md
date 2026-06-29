# AutostopManager Deployment Runbook

Purpose: keep the manager memory/MCP layer reproducible without leaking CRM
data.

## What Can Be Published

Safe to commit and push:

- Python package code in `autostop_manager/`
- tests in `tests/`
- playbooks and source-routing catalogs in `docs/`
- project metadata such as `agent.md`, `AGENTS.md`, `README.md`,
  `pyproject.toml`, `.gitignore`

Do not commit or push:

- `data/` runtime artifacts
- SQLite memory databases
- CRM board snapshots
- client names, phones, payments, VIN/license snapshots, or repair-order dumps
- temporary web/browser/test caches

## Local Verification

Run before publishing:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m pytest -q
```

Optional manual checks:

```powershell
python -m autostop_manager.cli today
python -m autostop_manager.cli service-plan --area parts --city Красноярск --vehicle "Lexus RX200T" --part-number 90311-89014 --urgency today
python -m autostop_manager.cli knowledge-probe "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM"
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
```

## GitHub Publish Checklist

Use this order when the owner asks to clean up, update docs, publish to GitHub,
and deploy:

1. Update `agent.md`, `README.md`, `docs/agent/knowledge_base_index.md`,
   `docs/agent/knowledge_shelves.md`, playbooks, source catalogs, and tests for
   any new commands or route cards.
2. Run `python -m autostop_manager.cli knowledge-sync`.
3. Run `python -m autostop_manager.cli knowledge-audit` and confirm
   `missing_files=[]` and `warnings=[]`. `optional_missing_files` may list
   `data/private_knowledge/*` in a clean checkout; restore those local runtime
   files only when a business-identity task needs exact current private facts.
4. Run `python -m pytest -q`.
5. Check `git status --short --ignored` and confirm `data/`, caches, SQLite
   files, runtime snapshots, credentials, and CRM evidence are not staged.
6. Commit only code, tests, documentation, and safe owner-provided source packs.
7. Push the current branch to GitHub.

## GitHub SSH Access From Codex VPS

Verified on the production Codex/server shell on 2026-05-29.

The GitHub deploy key is already present on the server:

- SSH config: `/root/.ssh/config`
- Host alias: `github.com-autostopcrm`
- Identity file: `/root/.ssh/autostopcrm_github`
- Repository SSH URL:
  `git@github.com-autostopcrm:UgaChavis/AutostopManager.git`

Important: this key is currently a read-only deploy key for GitHub. It is good
for authentication checks and fetch/pull operations. A direct `git push` with
this key fails with `The key you are authenticating with has been marked as read
only.`

Do not use the HTTPS remote for unattended operations from this shell; it will
fail with `could not read Username for 'https://github.com'` unless a separate
credential helper is configured. Do not assume raw `git@github.com` will pick
the right key either; use the configured alias.

Verify access without printing secrets:

```bash
ssh -T github.com-autostopcrm
```

Expected result is the normal GitHub authentication greeting for
`UgaChavis/GITHUB`; GitHub still says it does not provide shell access.

If `origin` points at HTTPS, fix it once:

```bash
git remote set-url origin git@github.com-autostopcrm:UgaChavis/AutostopManager.git
```

Normal server sync from `/opt/AutostopManager`:

```bash
git status --short
git branch --show-current
git fetch origin AutostopManager
```

For publishing, use one of these write-capable paths:

- temporarily configure a GitHub write credential/token for `git push`;
- use a separate write-enabled SSH key/alias;
- use the GitHub connector/API from Codex when available.

Keep private keys out of Git and never paste key contents into chat, docs, CRM,
or logs.

## Local MCP Server

Default local command:

```powershell
python -m autostop_manager.mcp_server
```

Optional environment:

```powershell
$env:AUTOSTOP_MANAGER_MCP_HOST = "127.0.0.1"
$env:AUTOSTOP_MANAGER_MCP_PORT = "41931"
$env:AUTOSTOP_MANAGER_MCP_PATH = "/mcp"
$env:AUTOSTOP_MANAGER_DB = "C:\path\to\autostop_manager.sqlite3"
```

## Production Server Deployment

Verified production shape on 2026-05-29:

- host: `vps26457.mnogoweb.in`
- AutostopManager checkout: `/opt/AutostopManager`
- CRM checkout: `/opt/autostopcrm`
- CRM compose file: `/opt/autostopcrm/docker-compose.yml`
- CRM deploy script: `/opt/autostopcrm/deploy.sh`
- CRM MCP endpoint inside container: `http://127.0.0.1:41831/mcp`
- host port for MCP: `127.0.0.1:8001`
- public MCP endpoint: `https://crm.autostopcrm.ru/mcp`

The CRM Docker compose mounts AutostopManager into the CRM container:

```yaml
AUTOSTOP_MANAGER_PATH: /opt/AutostopManager
```

There is no separate systemd service for AutostopManager in this setup. The
manager MCP tools are loaded by the CRM container when the manager checkout is
mounted and `autostop_manager.mcp_tools` can be imported.

Server update flow for AutostopManager-only changes:

```bash
cd /opt/AutostopManager
git switch AutostopManager
git pull --ff-only origin AutostopManager
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli annotations-audit
python -m autostop_manager.cli system-audit

cd /opt/autostopcrm
AUTOSTOP_SKIP_GIT_SYNC=1 ./deploy.sh
```

Use `AUTOSTOP_SKIP_GIT_SYNC=1` when only the mounted AutostopManager checkout
changed and the CRM checkout should not be reset. Omit it only when the CRM repo
itself must be synced from `origin/autostopcrm-v1`.

Do not push private runtime data to make deployment appear complete.

## Server Smoke Check

After deployment, verify:

- service process is running
- MCP endpoint answers on configured host/port/path
- `today_context` works
- `probe_knowledge_base` works for a known new route such as DSG, ECU/KOMBI, or
  Krasnoyarsk parts sourcing
- `audit_knowledge_base` returns no required missing files or warnings
- `recommend_service_management_actions` works
- logs do not contain secrets or CRM dumps

Useful production checks:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
cd /opt/autostopcrm
docker compose exec -T autostopcrm python scripts/check_live_connector.py \
  --strict \
  --skip-public-site \
  --skip-public-write-protection \
  --local-api-url http://127.0.0.1:41731 \
  --mcp-url http://127.0.0.1:41831/mcp \
  --expect-admin
```
