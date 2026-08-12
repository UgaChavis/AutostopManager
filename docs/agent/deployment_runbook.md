# AutostopManager Deployment Runbook

Canonical verification, GitHub publication, production deploy and rollback
route. Publish only code, tests, playbooks, route/source catalogs and project
metadata. Never publish `data/`, SQLite, CRM/Store/Gmail records, customer
identifiers, money data, secrets, OAuth state or generated private output.

## Release Gates

Run once on the final tree:

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
.venv/bin/python -m coverage report --fail-under=82
git diff --check
```

`missing_files` and warnings must be empty; absent optional private knowledge is
allowed. Inspect `git status --short --ignored` and stage only intended safe
files. A dirty production/CRM checkout is never reset, rebased or deployed
blindly.

## GitHub Publication

The workstation is the publisher; the configured Git credential helper owns
HTTPS authentication. `gh auth status` is irrelevant to normal Git push.

```bash
git fetch origin AutostopManager --prune
git merge-base --is-ancestor origin/AutostopManager HEAD
git push origin HEAD:AutostopManager
git rev-parse HEAD
git ls-remote origin refs/heads/AutostopManager
```

Fetch and integrate any concurrent remote commit intentionally, rerun affected
gates, and never force-push the production branch. The final local and remote
hashes must match. Do not print credential-helper state, tokens or credentialed
URLs.

## Production Shape

- Manager checkout: `/opt/AutostopManager`, branch `AutostopManager`.
- CRM checkout: `/opt/autostopcrm`, branch `autostopcrm-v1`.
- CRM deploy: `/opt/autostopcrm/deploy.sh`.
- CRM host API/MCP: `127.0.0.1:8000` / `127.0.0.1:8001`.
- CRM container API/MCP: `127.0.0.1:41731` /
  `http://127.0.0.1:41831/mcp`.
- Public MCP: `https://crm.autostopcrm.ru/mcp`.
- Store: `/opt/autostop-app`, public site `https://autostop24.shop`.
- Immutable Manager releases: `/opt/autostop-manager-releases/`; `current`
  points atomically to the active read-only snapshot while SQLite is overlaid
  from the persistent Manager data directory.

CRM loads Manager tools from that immutable release. The hardened
`autostop-integration-audit.timer` also runs from `current`; reinstall its unit
only when the unit contract changes. CRM and Store share only the dedicated
internal agent network; neither Manager nor CRM receives Store DB access.

## Deploy

Preflight the live checkouts and rollback state:

```bash
cd /opt/AutostopManager
git status --short --branch
git fetch origin AutostopManager
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/AutostopManager)"

cd /opt/autostopcrm
git status --short --branch
git fetch origin autostopcrm-v1
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/autostopcrm-v1)"
./deploy.sh
```

The CRM deploy script must keep the CRM checkout clean, create rollback data,
prebuild the immutable image, snapshot Manager, preserve `.env`, uploads and
PostgreSQL volumes, replace only the CRM service in the bounded window, and run
internal/public Gateway smoke. There is no Git-sync bypass.

For a Store-affecting release, use backup -> Store API/auth/migration -> pure
read/service-scope checks -> internal network -> Manager snapshot -> CRM
Gateway. Store failure must degrade only Store. Never create a customer order
as smoke or perform supplier procurement without a separate exact command.

## Post-Deploy Verification

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
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit --full
./scripts/doctor.sh --full
```

Confirm:

- exactly 24 public Gateway v2 tools and 77 Manager raw tools;
- protected OAuth/PKCE/refresh behavior and both internal/public smoke;
- knowledge/system audits and capability matrices have no gaps;
- CRM reads survive Store degradation and Store GETs are mutation-free;
- no raw payloads or secrets appear in logs;
- CRM, public Gateway, AutoStop App/site, VPN, nginx and required systemd units
  remain healthy;
- the deployed Manager revision equals GitHub and rollback refs exist.

Production Store management smoke stays dry-run unless an explicitly approved,
safe and reversible synthetic object exists. Record only compact ids, counts,
versions, health booleans and rollback refs.

## Failure And Rollback

Stop on a dirty/unmatched checkout, failed backup, failed schema/capability
gate, tool-count drift, missing rollback proof or unhealthy preflight. Do not
hide failures by editing docs, bypassing Git sync or changing secrets. If the
bounded deploy fails after replacement, use the deploy script's verified
rollback assets, then rerun container, endpoint, OAuth and Gateway checks before
declaring service restored.
