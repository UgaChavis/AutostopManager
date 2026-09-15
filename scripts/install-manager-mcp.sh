#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
RUNTIME_PYTHON="/opt/AutostopManager/.venv/bin/python"
RUNTIME_ENV="/opt/AutostopManager/.env"
RUNTIME_DB="/opt/AutostopManager/data/autostop_manager.sqlite3"
UNIT_NAME="autostop-manager-mcp.service"
UNIT_SOURCE="${PROJECT_ROOT}/deploy/systemd/${UNIT_NAME}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
activate=0
replace_unit=0

usage() {
  echo "usage: $0 [--activate] [--replace-unit]" >&2
}

wait_for_mcp_socket() {
  local attempt
  for attempt in {1..50}; do
    systemctl is-active --quiet "${UNIT_NAME}" || return 1
    if (exec 3<>/dev/tcp/127.0.0.1/41931) 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate)
      activate=1
      ;;
    --replace-unit)
      replace_unit=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl_required=true" >&2
  exit 1
fi
if [[ ! -d "${RELEASE_ROOT}" ]]; then
  echo "manager_release_unavailable=true" >&2
  exit 1
fi
if [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]]; then
  echo "current_release_source_required=true" >&2
  exit 1
fi
if [[ ! -f "${UNIT_SOURCE}" || -L "${UNIT_SOURCE}" ]]; then
  echo "manager_mcp_unit_source_invalid=true" >&2
  exit 1
fi
if [[ ! -x "${RUNTIME_PYTHON}" || ! -f "${RUNTIME_ENV}" || -L "${RUNTIME_ENV}" ]]; then
  echo "manager_mcp_runtime_prerequisite_invalid=true" >&2
  exit 1
fi
if [[ ! -d /opt/AutostopManager/data || -L /opt/AutostopManager/data ]]; then
  echo "manager_mcp_state_directory_invalid=true" >&2
  exit 1
fi
if [[ -L "${UNIT_PATH}" || ( -e "${UNIT_PATH}" && ! -f "${UNIT_PATH}" ) ]]; then
  echo "manager_mcp_installed_unit_invalid=true" >&2
  exit 1
fi
if [[ -f "${UNIT_PATH}" && "${replace_unit}" -ne 1 ]] \
  && ! cmp -s "${UNIT_SOURCE}" "${UNIT_PATH}"; then
  echo "manager_mcp_installed_unit_differs=true" >&2
  echo "manager_mcp_replace_unit_required=true" >&2
  exit 1
fi

install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNIT_PATH}"
fi

echo "manager_mcp_unit_installed=true"
echo "activation_requested=$([[ "${activate}" -eq 1 ]] && echo true || echo false)"

if [[ "${activate}" -ne 1 ]]; then
  exit 0
fi

if systemctl is-active --quiet "${UNIT_NAME}"; then
  systemctl enable "${UNIT_NAME}"
  systemctl restart "${UNIT_NAME}"
else
  systemctl enable --now "${UNIT_NAME}"
fi
systemctl is-active --quiet "${UNIT_NAME}"
if ! wait_for_mcp_socket; then
  echo "manager_mcp_listener_not_ready=true" >&2
  exit 1
fi

env \
  PYTHONPATH="${RELEASE_ROOT}" \
  PYTHONSAFEPATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  AUTOSTOP_MANAGER_ENV_FILE=/dev/null \
  AUTOSTOP_MANAGER_DB="${RUNTIME_DB}" \
  "${RUNTIME_PYTHON}" -m autostop_manager.cli mcp-probe \
    --url http://127.0.0.1:41931/mcp \
    --provider-failure-check

echo "manager_mcp_active=true"
