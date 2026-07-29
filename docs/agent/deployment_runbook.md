# AutostopManager Deployment Runbook

Purpose: keep the manager memory/MCP/store-adapter layer reproducible without
leaking CRM or AutoStop App data.

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
- store orders, customer contacts, line items, stock rows, warehouse dumps, or
  raw API payloads
- client names, phones, payments, VIN/license snapshots, or repair-order dumps
- temporary web/browser/test caches

## Local Verification

Run before publishing:

```bash
.venv/bin/python -m autostop_manager.cli knowledge-sync
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli cleanup-audit
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check autostop_manager tests
.venv/bin/python -m mypy autostop_manager
.venv/bin/python -m pytest -q
.venv/bin/python -m coverage run -m pytest -q
.venv/bin/python -m coverage report
node --check frontend/control-center/app.js
```

Optional manual checks:

```bash
.venv/bin/python -m autostop_manager.cli today
.venv/bin/python -m autostop_manager.cli service-plan --area parts --city Красноярск --vehicle "Lexus RX200T" --part-number 90311-89014 --urgency today
.venv/bin/python -m autostop_manager.cli knowledge-probe "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM"
.venv/bin/python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
.venv/bin/python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
.venv/bin/python -m autostop_manager.cli knowledge-search "route card aliases source_of_truth_files" --domain knowledge_intake
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
4. Run `annotations-audit`, `skills-audit`, and `cleanup-audit`; all must be
   green before publish.
5. Run Ruff check/format verification, Mypy, `python -m pytest -q`, branch
   coverage with `coverage report`, and the Node syntax check shown above.
6. Check `git status --short --ignored` and confirm `data/`, caches, SQLite
   files, runtime snapshots, credentials, CRM/store evidence, and API payloads
   are not staged.
7. Commit only code, tests, documentation, and safe owner-provided source packs.
8. Push the checked-out commit from the workstation with the explicit
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

```bash
.venv/bin/python -m autostop_manager.mcp_server
```

Optional environment:

```powershell
$env:AUTOSTOP_MANAGER_MCP_HOST = "127.0.0.1"
$env:AUTOSTOP_MANAGER_MCP_PORT = "41931"
$env:AUTOSTOP_MANAGER_MCP_PATH = "/mcp"
$env:AUTOSTOP_MANAGER_DB = "C:\path\to\autostop_manager.sqlite3"
```

Store adapter configuration is runtime-only:

```powershell
$env:AUTOSTOP_STORE_API_URL = "http://autostop-app:8000"
$env:AUTOSTOP_STORE_READ_TOKEN = "<runtime-secret>"
$env:AUTOSTOP_STORE_QUOTE_TOKEN = "<runtime-secret>"
$env:AUTOSTOP_STORE_MANAGE_TOKEN = "<runtime-secret>"
$env:AUTOSTOP_STORE_OWNER_TOKEN = "<runtime-secret>"
```

Never print these token values, bake them into an image, commit them, or reuse a
human ADMIN password. AutoStop App stores only service-principal hash/metadata.
The Manager client appends `/internal/agent/v1`, uses the read token for general
GET, the quote token for exact full quote/sourcing reads, and the manage token
for the seven optimized named actions; the independent owner principal covers
the full employee OpenAPI through the guarded transport. Manager has no Store
database access and never reads the Store application's `.env`.

The hidden read-only `get_store_analytics_report` capability uses that same
internal URL and `store:read` token for the DB-backed aggregate report. Never
point it at `autostop24.shop` or any other public/external host. It remains
available only through guarded raw discovery and does not change the public
Gateway count of 24 tools.

## Production Server Deployment

Verified production shape on 2026-07-14:

- host: `vps26457.mnogoweb.in`
- AutostopManager checkout: `/opt/AutostopManager`
- CRM checkout: `/opt/autostopcrm`
- CRM compose file: `/opt/autostopcrm/docker-compose.yml`
- CRM deploy script: `/opt/autostopcrm/deploy.sh`
- CRM API inside container: `http://127.0.0.1:41731`
- CRM MCP endpoint inside container: `http://127.0.0.1:41831/mcp`
- host ports: `127.0.0.1:8000 -> 41731` (API), `127.0.0.1:8001 -> 41831` (MCP)
- public MCP endpoint: `https://crm.autostopcrm.ru/mcp`
- AutoStop App production directory: `/opt/autostop-app`
- AutoStop App public site: `https://autostop24.shop`

The deploy script creates an immutable Manager snapshot under
`/opt/autostop-manager-releases/` and atomically points
`/opt/autostop-manager-releases/current` at it. Docker mounts that current
release read-only and overlays only the Manager SQLite data directory.

The `41731` and `41831` addresses are container-local. From the production
host use ports `8000` and `8001`; external clients use only the public HTTPS
endpoint and OAuth. Do not put a container-local URL into a ChatGPT/App
connector configuration.

```yaml
AUTOSTOP_MANAGER_PATH: /opt/AutostopManager
```

Manager MCP tools are loaded by the CRM container when the manager checkout is
mounted and `autostop_manager.mcp_tools` can be imported. The only standalone
Manager unit is the hardened hourly `autostop-integration-audit.timer`; it runs
the same immutable Manager release exposed through
`/opt/autostop-manager-releases/current`, performs read-only CRM/Store/web
checks, and validates the current root-only Gmail proof. The checkout venv is
only the Python runtime; imports resolve from the release working directory.
Install or refresh it with `scripts/install-integration-audit-timer.sh` after
the corresponding CRM release is live.
The audit also executes the CRM and Store machine-verifiable capability
matrices with `--require-complete`; a new human UI/API action without an
explicit Gateway path or reviewed human-session exemption fails closed.

CRM/Gateway and AutoStop App must share only the dedicated Docker integration
network needed for the internal agent API. Keep the store database private to
the store network, do not publish the internal agent API to the internet, and
do not grant Manager direct database connectivity. Preserve production `.env`,
uploads, and PostgreSQL volumes during store deploy.

Release flow: complete **Local Verification** above in the clean Manager
release checkout, then verify both pinned revisions and deploy:

```bash
cd /opt/AutostopManager
git switch AutostopManager
git status --short
git fetch origin AutostopManager
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/AutostopManager)"

cd /opt/autostopcrm
git fetch origin autostopcrm-v1
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/autostopcrm-v1)"
./deploy.sh
```

The deploy script requires a clean CRM checkout, verifies its remote revision,
prebuilds an immutable image, snapshots Manager, provisions stable production
OAuth, rotates only the internal compatibility bearer,
creates and verifies rollback data, replaces only the CRM service inside a
bounded maintenance window, and runs internal plus public Gateway v2 smoke.
There is no Git-sync bypass in the normal release path: both pinned branches
must be fetched, clean, and at the verified remote revision before plain
`./deploy.sh` is run.

For the store integration, use this release order: backup/rollback -> deploy
the store agent API/auth/migration -> verify pure reads and service scopes ->
attach the dedicated internal Docker network -> publish the immutable Manager
release -> deploy CRM/Gateway -> internal and public OAuth/Gateway smoke. Do not
deploy the Gateway adapter before the store read API and service principals are
ready. Store outage must remain a store-only degraded state throughout.

Do not push private runtime data to make deployment appear complete.

## Server Smoke Check

After deployment, verify:

- service process is running
- MCP endpoint answers on configured host/port/path
- exactly 24 Gateway v2 tools are visible and legacy tools are absent
- CRM and Store capability matrices report zero unreviewed gaps and valid
  UI/API/Gateway evidence
- Manager raw registry contains 77 tools, including Store INTERNAL_ONLY
  adapters and the guarded owner capability/API pair; none expands the public
  24-tool Gateway surface
- a Russian automotive technical query discovers only the relevant read-only
  source capability, while an exact protected/internal capability name remains
  undiscoverable
- OAuth protected-resource/server metadata, PKCE S256, owner approval, refresh
  rotation, audience/scopes, and a clear 401 challenge are verified
- a saved refresh session still works after a second deploy
- the standard Gateway v2 smoke passes internally and publicly
- the exhaustive safe smoke invokes all 24 tools and leaves its synthetic
  workflows terminal
- Manager knowledge, annotation, skill, and system audits pass
- logs do not contain secrets or CRM dumps
- store agent GETs are mutation-free; store digest/search/entity reads work;
  physical/reserved/available and multiple storage locations are represented
- bootstrap performs one Store snapshot request with no cursor/ACK and leaves
  digest checkpoints untouched; `agent_board_digest(scope=store)` uses the
  owner `store_digest` cursor and commits only after its final page
- store outage degrades only store state while a representative CRM read still
  succeeds
- production store management smoke is dry-run only unless a safe synthetic,
  reversible object is explicitly available; never create a customer order for
  smoke
- `get_store_analytics_report` is discoverable as a hidden read-only raw
  capability, returns aggregate-only `store_analytics_report_v1` through the
  internal App URL, and exposes no raw event or private identifier
- CRM, AutoStop App, `autostop24.shop`, public Gateway, and VPN remain healthy

Useful production checks:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
cd /opt/autostopcrm
docker compose exec -T autostopcrm python scripts/check_agent_gateway_v2.py \
  --mcp-url http://127.0.0.1:41831/mcp --exhaustive --require-store --require-web
docker compose exec -T autostopcrm python scripts/check_live_connector.py \
  --strict --site-url https://crm.autostopcrm.ru --expect-https \
  --local-api-url http://127.0.0.1:41731 --expect-admin
docker compose exec -T autostopcrm python scripts/check_mcp_oauth.py \
  --mcp-url https://crm.autostopcrm.ru/mcp
cd /opt/AutostopManager
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit --full
./scripts/doctor.sh --full
```

Also run the store-aware production smoke through the existing public Gateway:
`agent_bootstrap`, `agent_board_digest(scope="store")`, a bounded
`agent_search(entity="store_part")`, and one exact read-only
`agent_entity_context`. Verify a safe `agent_inventory_workflow` dry-run shows
its effects without applying them. Record only compact IDs, counts, versions,
health booleans, and rollback references in the deployment report.
