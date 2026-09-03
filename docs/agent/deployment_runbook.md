# AutostopManager Deployment Runbook

Canonical verification, GitHub publication, production deploy and rollback
route. Publish only code, tests, playbooks, route/source catalogs and project
metadata. Never publish `data/`, SQLite, CRM/Store/Gmail records, customer
identifiers, money data, secrets, OAuth state or generated private output.

## Release Gates

Run once on the final tree. The disposable database is mandatory: preflight
must never rewrite the persistent production Manager index.

```bash
run_manager_release_gates() (
  set -e
  local release_gate_tmp
  release_gate_tmp="$(mktemp -d /tmp/autostop-manager-release-gates.XXXXXX)"
  cleanup_release_gate_tmp() {
    [[ "$release_gate_tmp" == /tmp/autostop-manager-release-gates.* ]] || return 1
    rm -rf -- "$release_gate_tmp"
  }
  trap cleanup_release_gate_tmp EXIT
  export AUTOSTOP_MANAGER_DB="$release_gate_tmp/preflight.sqlite3"

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
- External Codex surface: the 24-tool CRM Gateway only. The standalone 77-tool
  Manager registry is internal inventory; production imports only its required
  Gateway subset and does not install it as a separate account App.
- Store: `/opt/autostop-app`, public site `https://autostop24.shop`.
- Immutable Manager releases: `/opt/autostop-manager-releases/`; `current`
  points atomically to the active read-only snapshot while SQLite is overlaid
  from the persistent Manager data directory.

CRM loads Manager tools from that immutable release. The hardened
`autostop-integration-audit.timer` also runs from `current`; reinstall its unit
only when the unit contract changes. CRM and Store share only the dedicated
internal agent network; neither Manager nor CRM receives Store DB access.

## Deploy

The current `/opt/autostopcrm/deploy.sh` is a coupled CRM + Manager release,
not a Manager-only deploy path. It validates live Store health and replaces the
CRM container even when only the Manager revision changed. Run it only after
the owner explicitly authorizes advancing the CRM checkout/restarting CRM to
the exact remote revision. Read-only Store health gates are part of every
coupled release.

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
# Watchdog installation is disabled by default; do not opt in here.
./deploy.sh

# Sync the host-owned scheduled audit from the activated immutable Manager release.
install -D -m 0644 \
  /opt/autostop-manager-releases/current/deploy/systemd/autostop-integration-audit.service \
  /etc/systemd/system/autostop-integration-audit.service
install -D -m 0644 \
  /opt/autostop-manager-releases/current/deploy/systemd/autostop-integration-audit.timer \
  /etc/systemd/system/autostop-integration-audit.timer
systemctl daemon-reload
cmp --silent \
  /opt/autostop-manager-releases/current/deploy/systemd/autostop-integration-audit.service \
  /etc/systemd/system/autostop-integration-audit.service
cmp --silent \
  /opt/autostop-manager-releases/current/deploy/systemd/autostop-integration-audit.timer \
  /etc/systemd/system/autostop-integration-audit.timer
systemctl show --property=LoadState --value autostop-integration-audit.service | grep -Fx loaded
systemctl show --property=LoadState --value autostop-integration-audit.timer | grep -Fx loaded
systemctl is-enabled --quiet autostop-integration-audit.timer
systemctl is-active --quiet autostop-integration-audit.timer
systemctl show --property=NextElapseUSecRealtime --value \
  autostop-integration-audit.timer | grep -Ev '^(n/a)?$'
```

A bare `./deploy.sh` no longer installs the production watchdog. Approved releases
must verify that the watchdog timer and service remain absent, and keep the CRM checkout clean.
Do not set `AUTOSTOP_INSTALL_WATCHDOG=1` without a separate exact owner authorization.
They create rollback data and a Manager snapshot, preserve `.env`/uploads/PostgreSQL volumes, and replace only CRM in the bounded window.
They must pass internal/public smoke. No Git-sync bypass.

The coupled deploy uses backup -> Store API/auth/migration -> pure
read/service-scope checks -> internal network -> Manager snapshot -> CRM
Gateway. The operator runs `integration-audit --full` in the post-deploy block
below. Store failure must degrade only Store. Never create a customer order as
smoke or perform supplier procurement without a separate exact command.

## Post-Deploy Verification

The checks below are part of the coupled release. Gateway `--exhaustive`,
`check_live_connector.py` without `--skip-mcp` and `integration-audit --full`
call `get_runtime_status`, which performs a live Store health read.
`check_mcp_oauth.py` does not read Store, but it creates, refreshes and revokes
OAuth state, so it is not a read-only maintenance check.

```bash
cd /opt/autostopcrm
docker compose ps autostopcrm
docker compose exec -T autostopcrm python scripts/check_agent_gateway_v2.py \
  --mcp-url http://127.0.0.1:41831/mcp --exhaustive --require-web
docker compose exec -T autostopcrm python scripts/check_live_connector.py \
  --strict --site-url https://crm.autostopcrm.ru --expect-https \
  --local-api-url http://127.0.0.1:41731 --expect-admin
docker compose exec -T autostopcrm python scripts/check_mcp_oauth.py \
  --mcp-url https://crm.autostopcrm.ru/mcp
cd /opt/AutostopManager
.venv/bin/python -m autostop_manager.cli control-report --format markdown
.venv/bin/python -m autostop_manager.cli integration-audit --full
./scripts/doctor.sh --full
cd /opt/autostop-manager-releases/current
/opt/AutostopManager/.venv/bin/python scripts/capture_public_camera.py --verify-runner
```

Confirm:

- exactly 24 public Gateway v2 tools and 77 internal Manager registry tools;
- protected OAuth/PKCE/refresh behavior and both internal/public smoke;
- knowledge/system audits and capability matrices have no gaps;
- no raw payloads or secrets appear in logs;
- CRM, public Gateway, VPN, nginx and required systemd units remain healthy;
- the deployed Manager revision equals GitHub and rollback refs exist.
- the public-camera non-root sandbox launches its pinned browser with networking
  disabled for the self-test; this check captures no camera frame.

Also confirm AutoStop App/site health, that CRM reads survive Store degradation,
and that Store GETs are mutation-free. Record only compact ids, counts,
versions, health booleans and rollback refs.

## Quick Maintenance

For a quick read-only maintenance check, do not use the full Gateway,
`check_live_connector.py` or `check_mcp_oauth.py`. Use the quick Manager,
container/nginx/HTTPS and Store-aware Gateway checks:

```bash
cd /opt/AutostopManager
.venv/bin/python -m autostop_manager.cli doctor
.venv/bin/python -m autostop_manager.cli control-report --format json
.venv/bin/python -m autostop_manager.cli integration-audit

cd /opt/autostopcrm
docker compose config --quiet
docker compose ps autostopcrm
docker compose exec -T autostopcrm python scripts/container_healthcheck.py
docker compose exec -T autostopcrm python scripts/check_agent_gateway_v2.py \
  --mcp-url http://127.0.0.1:41831/mcp --require-store
docker compose exec -T autostopcrm python scripts/check_agent_gateway_v2.py \
  --mcp-url https://crm.autostopcrm.ru/mcp --require-store
nginx -t
curl -fsS --max-time 8 -o /dev/null https://crm.autostopcrm.ru/
```

## Telegram-Only Release

Telegram bridge and local voice-recognition changes do not require a CRM or
Store deployment. `personal` and `work` use separate dedicated immutable
release roots, so an owner-authorized Telegram-only update cannot replace CRM
containers, the shared Manager release, or the other Telegram account.

After the normal Manager gates, publish the exact Manager commit to
`origin/AutostopManager`, install the Telegram Python dependencies and pinned
local model, then run:

```bash
cd /opt/AutostopManager
git fetch origin AutostopManager --prune
revision="$(git rev-parse origin/AutostopManager)"
# Work-only release: --account is mandatory and selects only the isolated work
# bridge.
sudo ./scripts/install-telegram-bridge.sh --account work --revision "$revision"
sudo ./scripts/provision-telegram-transcription-model.sh --account work --revision "$revision"
sudo ./scripts/deploy_telegram_bridge.sh --account work "$revision"
```

The script archives only that exact remote commit into a new root-owned
read-only release, atomically switches the selected Telegram account's
`current` symlink and installs only its unit. A work release that has not yet
been authorized remains inactive; an active selected account is restarted and
verified with the identity-free `probe`. The selected account also runs an
offline transcription-runtime check. On failure an active selected account is
rolled back at the release/unit and bridge-probe level. The independent
root-owned media model and venv are immutable checked inputs, not rollback
assets; if a media gate fails, treat media as unavailable until a corrected
work-only release succeeds. It does not deploy or restart CRM, Store, VPN,
nginx, the other Telegram account or other Manager consumers. A dirty Manager
working tree may remain untouched because the archive is built from the exact
published commit; never include uncommitted files in the release.

The installer places the versioned, reviewed root-owned checksum manifest before
the work provisioner runs. The provisioner accepts only an exact match to that
manifest, copies the fixed model payload through a root-owned staging directory
on the target filesystem, verifies the checksums again and installs a
root-owned read-only work model. It never uses personal-model cache metadata as
a trust input.

## Failure And Rollback

Stop on a dirty/unmatched checkout, failed backup, failed schema/capability
gate, tool-count drift, missing rollback proof or unhealthy preflight. Do not
hide failures by editing docs, bypassing Git sync or changing secrets. If the
bounded deploy fails after replacement, use the deploy script's verified
rollback assets, then rerun container, endpoint, OAuth and Gateway checks before
declaring service restored.
