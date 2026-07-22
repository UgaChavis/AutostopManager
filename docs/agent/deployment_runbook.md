# AutostopManager Deployment Runbook

Purpose: keep the manager memory/MCP layer reproducible without leaking CRM
data.

## What Can Be Published

Safe to commit and push:

- Python package code in `autostop_manager/`
- tests in `tests/`
- playbooks and source-routing catalogs in `docs/`
- project metadata such as `AGENTS.md`, `README.md`,
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
python -m ruff check .
python -m ruff format --check autostop_manager tests
python -m mypy autostop_manager
python -m pytest -q
python -m coverage run -m pytest -q
python -m coverage report
node --check frontend/control-center/app.js
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

1. Update `AGENTS.md`, `README.md`, `docs/agent/knowledge_shelves.md`,
   playbooks, source catalogs, and tests for
   any new commands or route cards.
2. Run `python -m autostop_manager.cli knowledge-sync`.
3. Run `python -m autostop_manager.cli knowledge-audit` and confirm
   `missing_files=[]` and `warnings=[]`. `optional_missing_files` may list
   `data/private_knowledge/*` in a clean checkout; restore those local runtime
   files only when a business-identity task needs exact current private facts.
4. Run Ruff check/format verification, Mypy, `python -m pytest -q`, branch
   coverage with `coverage report`, and the Node syntax check shown above.
5. Check `git status --short --ignored` and confirm `data/`, caches, SQLite
   files, runtime snapshots, credentials, and CRM evidence are not staged.
6. Commit only code, tests, documentation, and safe owner-provided source packs.
7. Push the checked-out commit from the workstation with the explicit
   `HEAD:AutostopManager` refspec below.

## GitHub Publish From The Workstation

Normal publishing uses Git over HTTPS through the configured Git credential
helper (Windows Git Credential Manager on the maintained workstation). GitHub
CLI authentication is separate: a failing `gh auth status` must not block
`git fetch` or `git push`. Use `gh auth login` only for operations performed by
`gh`, such as creating a pull request or working with GitHub issues.

After the intended files pass checks and are committed:

```powershell
git status --short --branch
git branch --show-current
git fetch origin AutostopManager --prune
git merge-base --is-ancestor origin/AutostopManager HEAD
if ($LASTEXITCODE -ne 0) { throw "origin/AutostopManager is not an ancestor of HEAD" }
git push origin HEAD:AutostopManager
git rev-parse HEAD
git ls-remote origin refs/heads/AutostopManager
```

Use `git push -u origin HEAD:AutostopManager` only when the local upstream is
missing. Use `git push --dry-run origin HEAD:AutostopManager` only while
diagnosing authentication or refspec routing; routine publishing does not need
both a dry-run and a real push. The final two hashes must match.

If another commit reaches GitHub first, fetch and inspect it, integrate it
intentionally, and rerun checks affected by the changed tree. Never force-push
the production branch. Treat `git remote -v` as runtime truth; do not rewrite a
working remote or print credential-helper output, private keys, tokens, or URLs
containing credentials.

The production server is a fetch/deploy target, not the normal publisher. On
the server, fetch the branch and verify exact revision parity; do not push from
`/opt/AutostopManager` and do not reset a dirty parallel checkout.

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

Verified production shape on 2026-07-14:

- host: `vps26457.mnogoweb.in`
- AutostopManager checkout: `/opt/AutostopManager`
- CRM checkout: `/opt/autostopcrm`
- CRM compose file: `/opt/autostopcrm/docker-compose.yml`
- CRM deploy script: `/opt/autostopcrm/deploy.sh`
- CRM MCP endpoint inside container: `http://127.0.0.1:41831/mcp`
- host port for MCP: `127.0.0.1:8001`
- public MCP endpoint: `https://crm.autostopcrm.ru/mcp`

The deploy script creates an immutable Manager snapshot under
`/opt/autostop-manager-releases/` and atomically points
`/opt/autostop-manager-releases/current` at it. Docker mounts that current
release read-only and overlays only the Manager SQLite data directory.

```yaml
AUTOSTOP_MANAGER_PATH: /opt/AutostopManager
```

There is no separate systemd service for AutostopManager in this setup. The
manager MCP tools are loaded by the CRM container when the manager checkout is
mounted and `autostop_manager.mcp_tools` can be imported.

Release flow:

```bash
cd /opt/AutostopManager
git switch AutostopManager
.venv/bin/python -m autostop_manager.cli knowledge-sync
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check autostop_manager tests
.venv/bin/python -m mypy autostop_manager
.venv/bin/python -m pytest -q
.venv/bin/python -m coverage run -m pytest -q
.venv/bin/python -m coverage report
node --check frontend/control-center/app.js
git status --short
git fetch origin AutostopManager
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/AutostopManager)"

cd /opt/autostopcrm
git fetch origin autostopcrm-v1
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/autostopcrm-v1)"
./deploy.sh
```

The deploy script requires a clean CRM checkout, verifies its remote revision,
prebuilds an immutable image, snapshots Manager, rotates connector auth,
creates and verifies rollback data, replaces only the CRM service inside a
bounded maintenance window, and runs internal plus public Gateway v2 smoke.
`AUTOSTOP_SKIP_GIT_SYNC=1` is an exceptional recovery override, not the normal
release path.

Do not push private runtime data to make deployment appear complete.

## Server Smoke Check

After deployment, verify:

- service process is running
- MCP endpoint answers on configured host/port/path
- exactly 24 Gateway v2 tools are visible and legacy tools are absent
- the standard Gateway v2 smoke passes internally and publicly
- the exhaustive safe smoke invokes all 24 tools and leaves its synthetic
  workflows terminal
- Manager knowledge, annotation, skill, and system audits pass
- logs do not contain secrets or CRM dumps

Useful production checks:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
cd /opt/autostopcrm
docker compose exec -T autostopcrm python scripts/check_agent_gateway_v2.py \
  --mcp-url http://127.0.0.1:41831/mcp --exhaustive
cd /opt/AutostopManager
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli control-report --format markdown
./scripts/doctor.sh --full
```
