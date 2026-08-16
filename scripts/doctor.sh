#!/usr/bin/env bash
set -Eeuo pipefail

MANAGER_ROOT="${AUTOSTOP_MANAGER_ROOT:-/opt/AutostopManager}"
CRM_ROOT="${AUTOSTOP_CRM_ROOT:-/opt/autostopcrm}"
CRM_API_URL="${AUTOSTOP_CRM_API_URL:-http://127.0.0.1:8000}"
CRM_MCP_URL="${AUTOSTOP_CRM_MCP_URL:-http://127.0.0.1:8001/mcp}"
CRM_PUBLIC_MCP_URL="${AUTOSTOP_CRM_PUBLIC_MCP_URL:-https://crm.autostopcrm.ru/mcp}"
MANAGER_PYTEST_BASETEMP="${AUTOSTOP_MANAGER_PYTEST_BASETEMP:-$MANAGER_ROOT/tmp/pytest-manager}"
CRM_PYTEST_BASETEMP="${AUTOSTOP_CRM_PYTEST_BASETEMP:-$CRM_ROOT/.tmp-pytest}"
FULL=0

if [[ "${1:-}" == "--full" ]]; then
  FULL=1
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Usage: scripts/doctor.sh [--full]

Quick checks:
  - system tools, /tmp, Docker/Git/GitHub, Chromium/document tooling
  - AutostopManager doctor, control/environment reports, MCP import, ruff, pre-commit hook
  - AutoStopCRM ruff, MCP import, Playwright, docs/code audits, JS syntax, browser smoke
  - production CRM read-only API/runtime, Docker Compose, nginx config, and watchdog status

--full also runs both pytest suites.
USAGE
  exit 0
elif [[ -n "${1:-}" ]]; then
  echo "Unknown argument: $1" >&2
  exit 2
fi

status=0

section() {
  printf '\n== %s ==\n' "$1"
}

run() {
  local label="$1"
  shift
  printf '[..] %s\n' "$label"
  if "$@"; then
    printf '[OK] %s\n' "$label"
  else
    local code=$?
    printf '[!!] %s (exit %s)\n' "$label" "$code" >&2
    status=1
  fi
}

skip() {
  printf '[SKIP] %s\n' "$1"
}

env_file_value() {
  local file="$1"
  local key="$2"
  local line value
  [[ -f "$file" ]] || return 1
  line="$(grep -m1 -E "^${key}=" "$file" 2>/dev/null || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  value="${value%$'\r'}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

require_tool() {
  local tool="$1"
  run "tool: ${tool}" command -v "$tool"
}

system_chromium_version() {
  local bin output
  for bin in chromium chromium-browser google-chrome; do
    command -v "$bin" >/dev/null 2>&1 || continue
    output="$("$bin" --version 2>&1)" || continue
    # Ubuntu's chromium-browser snap shim can exit successfully while only
    # asking to install Chromium. Treat it as unavailable rather than green.
    if [[ "$output" =~ ^(Chromium|Google[[:space:]]Chrome) ]]; then
      printf '%s\n' "$output"
      return 0
    fi
  done
  return 1
}

section "System"
for tool in python3 git docker curl ss pdftotext pdftoppm gs libreoffice 7z node npm nginx; do
  require_tool "$tool"
done
run "/tmp writable" bash -c 'tmp="$(mktemp /tmp/autostop-doctor.XXXXXX)" && rm -f "$tmp"'
run "docker daemon" docker info
run "gh auth status" gh auth status
if system_chromium_version; then
  printf '[OK] system Chromium version\n'
else
  skip "system Chromium launcher unavailable; CRM Playwright browser smoke is authoritative"
fi

section "AutostopManager"
run "manager git status" git -C "$MANAGER_ROOT" status --short --branch
run "manager doctor" bash -c '"$1" -m autostop_manager.cli doctor >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager control report" bash -c '"$1" -m autostop_manager.cli control-report --format json >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager environment report" bash -c '"$1" -m autostop_manager.cli environment-report --format json >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager memory review" bash -c '"$1" -m autostop_manager.cli memory-review >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager provider smoke" bash -c '"$1" -m autostop_manager.cli provider-smoke --provider all --mode dry-run >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager knowledge intake smoke" bash -c '"$1" -m autostop_manager.cli knowledge-intake --path docs/agent/knowledge_map.json --dry-run >/dev/null' _ "$MANAGER_ROOT/.venv/bin/python"
run "manager MCP import" "$MANAGER_ROOT/.venv/bin/python" -c 'from mcp.server.fastmcp import FastMCP'
run "manager ruff" "$MANAGER_ROOT/.venv/bin/ruff" check "$MANAGER_ROOT"
if [[ -f "$MANAGER_ROOT/.pre-commit-config.yaml" ]]; then
run "manager pre-commit config" "$MANAGER_ROOT/.venv/bin/pre-commit" validate-config "$MANAGER_ROOT/.pre-commit-config.yaml"
run "manager pre-commit hook installed" test -x "$MANAGER_ROOT/.git/hooks/pre-commit"
else
  printf '[!!] manager pre-commit config missing\n' >&2
  status=1
fi
if [[ "$FULL" -eq 1 ]]; then
  mkdir -p "$(dirname "$MANAGER_PYTEST_BASETEMP")"
  run "manager pytest" "$MANAGER_ROOT/.venv/bin/python" -m pytest "$MANAGER_ROOT" --basetemp "$MANAGER_PYTEST_BASETEMP"
fi

section "AutoStopCRM"
run "crm git status" git -C "$CRM_ROOT" status --short --branch
run "crm MCP import" "$CRM_ROOT/.venv/bin/python" -c 'from mcp.server.fastmcp import FastMCP'
run "crm playwright version" "$CRM_ROOT/.venv/bin/python" -m playwright --version
run "crm ruff" "$CRM_ROOT/.venv/bin/ruff" check "$CRM_ROOT"
if [[ -f "$CRM_ROOT/.pre-commit-config.yaml" ]]; then
run "crm pre-commit config" "$CRM_ROOT/.venv/bin/pre-commit" validate-config "$CRM_ROOT/.pre-commit-config.yaml"
run "crm pre-commit hook installed" test -x "$CRM_ROOT/.git/hooks/pre-commit"
else
  printf '[!!] crm pre-commit config missing\n' >&2
  status=1
fi
run "crm docs audit" "$CRM_ROOT/.venv/bin/python" "$CRM_ROOT/scripts/docs_audit.py"
run "crm code audit" "$CRM_ROOT/.venv/bin/python" "$CRM_ROOT/scripts/code_health_audit.py"
run "crm generated JS syntax" "$CRM_ROOT/.venv/bin/python" "$CRM_ROOT/scripts/check_web_assets_js.py"
run "crm browser smoke" "$CRM_ROOT/.venv/bin/python" "$CRM_ROOT/scripts/browser_smoke.py"
CRM_RUNTIME_OPERATOR_USERNAME="${AUTOSTOP_CRM_OPERATOR_USERNAME:-}"
CRM_RUNTIME_OPERATOR_PASSWORD="${AUTOSTOP_CRM_OPERATOR_PASSWORD:-}"
if [[ -z "$CRM_RUNTIME_OPERATOR_USERNAME" ]]; then
  CRM_RUNTIME_OPERATOR_USERNAME="$(env_file_value "$CRM_ROOT/.env" "AUTOSTOP_SMOKE_OPERATOR_USERNAME" || true)"
fi
if [[ -z "$CRM_RUNTIME_OPERATOR_PASSWORD" ]]; then
  CRM_RUNTIME_OPERATOR_PASSWORD="$(env_file_value "$CRM_ROOT/.env" "AUTOSTOP_SMOKE_OPERATOR_PASSWORD" || true)"
fi
if [[ -n "$CRM_RUNTIME_OPERATOR_USERNAME" && -n "$CRM_RUNTIME_OPERATOR_PASSWORD" ]]; then
  run "crm runtime check" "$CRM_ROOT/.venv/bin/python" "$CRM_ROOT/scripts/check_agent_runtime.py" \
    --local-api-url "$CRM_API_URL" \
    --operator-username "$CRM_RUNTIME_OPERATOR_USERNAME" \
    --operator-password "$CRM_RUNTIME_OPERATOR_PASSWORD"
else
  skip "crm runtime check needs AUTOSTOP_CRM_OPERATOR_USERNAME/PASSWORD or AUTOSTOP_SMOKE_OPERATOR_USERNAME/PASSWORD in CRM .env"
fi
if [[ "$FULL" -eq 1 ]]; then
  mkdir -p "$(dirname "$CRM_PYTEST_BASETEMP")"
  run "crm pytest" bash -c 'cd "$1" && PYTHONPATH="$1/src:$1${PYTHONPATH:+:$PYTHONPATH}" "$1/.venv/bin/python" -m pytest tests --basetemp "$2"' _ "$CRM_ROOT" "$CRM_PYTEST_BASETEMP"
fi

section "Production Read-Only"
if [[ -f "$CRM_ROOT/docker-compose.yml" ]]; then
  run "docker compose config" docker compose -f "$CRM_ROOT/docker-compose.yml" config --quiet
  run "docker compose ps" docker compose -f "$CRM_ROOT/docker-compose.yml" ps
fi
run "nginx config" nginx -t
if systemctl show autostopcrm-watchdog.timer --property=LoadState --value --no-pager | grep -qx loaded; then
  run "production watchdog timer disabled" bash -c \
    'systemctl is-enabled autostopcrm-watchdog.timer 2>/dev/null | grep -qx disabled'
  run "production watchdog timer inactive" bash -c \
    'systemctl is-active autostopcrm-watchdog.timer 2>/dev/null | grep -qx inactive'
  run "production watchdog service unit present" bash -c 'systemctl show autostopcrm-watchdog.service --property=LoadState --value | grep -qx loaded'
else
  skip "production watchdog timer not installed"
fi
if docker ps --format '{{.Names}}' | grep -qx 'autostopcrm'; then
  run "crm container healthcheck" docker exec autostopcrm python scripts/container_healthcheck.py
else
  skip "crm container healthcheck needs running autostopcrm container"
fi
run "crm public HTTPS" curl -fsS --max-time 8 -o /dev/null https://crm.autostopcrm.ru/
CRM_RUNTIME_MCP_TOKEN="${AUTOSTOP_CRM_MCP_BEARER_TOKEN:-}"
if [[ -z "$CRM_RUNTIME_MCP_TOKEN" ]]; then
  CRM_RUNTIME_MCP_TOKEN="$(env_file_value "$CRM_ROOT/.env" "MINIMAL_KANBAN_MCP_BEARER_TOKEN" || true)"
fi
if [[ -n "$CRM_RUNTIME_MCP_TOKEN" ]]; then
  HAD_MCP_BEARER_TOKEN="${MINIMAL_KANBAN_MCP_BEARER_TOKEN+x}"
  PREVIOUS_MCP_BEARER_TOKEN="${MINIMAL_KANBAN_MCP_BEARER_TOKEN:-}"
  export MINIMAL_KANBAN_MCP_BEARER_TOKEN="$CRM_RUNTIME_MCP_TOKEN"
  run "crm local Gateway v2" "$CRM_ROOT/.venv/bin/python" \
    "$CRM_ROOT/scripts/check_agent_gateway_v2.py" --mcp-url "$CRM_MCP_URL"
  run "crm public Gateway v2" "$CRM_ROOT/.venv/bin/python" \
    "$CRM_ROOT/scripts/check_agent_gateway_v2.py" --mcp-url "$CRM_PUBLIC_MCP_URL"
  if [[ -n "$HAD_MCP_BEARER_TOKEN" ]]; then
    export MINIMAL_KANBAN_MCP_BEARER_TOKEN="$PREVIOUS_MCP_BEARER_TOKEN"
  else
    unset MINIMAL_KANBAN_MCP_BEARER_TOKEN
  fi
else
  skip "crm Gateway v2 needs AUTOSTOP_CRM_MCP_BEARER_TOKEN or MINIMAL_KANBAN_MCP_BEARER_TOKEN in CRM .env"
fi
run "crm local board context" curl -fsS --max-time 8 -o /dev/null "$CRM_API_URL/api/get_board_context"

exit "$status"
