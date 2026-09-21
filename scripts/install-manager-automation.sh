#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
RUNTIME_PYTHON="/opt/AutostopManager/.venv/bin/python"
RUNTIME_STATE_DIR="/var/lib/autostop-manager-scheduler"
RUNTIME_DB="${RUNTIME_STATE_DIR}/registry.sqlite3"
UNIT_NAME="autostop-manager-scheduler.service"
UNIT_SOURCE="${PROJECT_ROOT}/deploy/systemd/${UNIT_NAME}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
GROUP_HELPER="${PROJECT_ROOT}/scripts/ensure-automation-group.sh"
CONFIG_DIR="/etc/autostop-manager"
CONFIG_PATH="${CONFIG_DIR}/automation-control.env"
SOCKET_PATH="/run/autostop-manager-automation/control.sock"
activate=0
activate_under_hold=0
replace_unit=0
release_attempt_key=""
manager_revision="${AUTOSTOP_MANAGER_REVISION:-}"
crm_revision="${AUTOSTOP_AUTOMATION_CRM_REVISION:-}"
crm_version="${AUTOSTOP_AUTOMATION_CRM_VERSION:-}"

usage() {
  echo "usage: $0 [--activate|--activate-under-hold] [--replace-unit] --manager-revision SHA [--crm-revision SHA] [--crm-version VERSION] [--release-attempt-key KEY]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) activate=1 ;;
    --activate-under-hold) activate=1; activate_under_hold=1 ;;
    --replace-unit) replace_unit=1 ;;
    --release-attempt-key)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      release_attempt_key="$1"
      ;;
    --manager-revision)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      manager_revision="$1"
      ;;
    --crm-revision)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      crm_revision="$1"
      ;;
    --crm-version)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      crm_version="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

if [[ -z "${manager_revision}" && -f "${PROJECT_ROOT}/REVISION" && ! -L "${PROJECT_ROOT}/REVISION" ]]; then
  manager_revision="$(<"${PROJECT_ROOT}/REVISION")"
fi
if [[ ! "${manager_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "manager_revision_invalid=true" >&2
  exit 2
fi
if [[ -n "${crm_revision}" && ! "${crm_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "crm_revision_invalid=true" >&2
  exit 2
fi
if [[ "${activate}" -eq 1 && ! "${crm_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "crm_revision_required_for_activation=true" >&2
  exit 2
fi
if [[ -n "${crm_version}" && ! "${crm_version}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]]; then
  echo "crm_version_invalid=true" >&2
  exit 2
fi
if [[ "${activate_under_hold}" -eq 1 && ! "${release_attempt_key}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "release_attempt_key_invalid=true" >&2
  exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl_required=true" >&2
  exit 1
fi
if [[ ! -d "${RELEASE_ROOT}" ]] || [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]]; then
  echo "current_release_source_required=true" >&2
  exit 1
fi
if [[ ! -f "${UNIT_SOURCE}" || -L "${UNIT_SOURCE}" \
  || ! -f "${GROUP_HELPER}" || -L "${GROUP_HELPER}" || ! -x "${GROUP_HELPER}" \
  || ! -x "${RUNTIME_PYTHON}" ]]; then
  echo "automation_runtime_prerequisite_invalid=true" >&2
  exit 1
fi
"${GROUP_HELPER}" >/dev/null
if [[ -L "${RUNTIME_STATE_DIR}" || ( -e "${RUNTIME_STATE_DIR}" && ! -d "${RUNTIME_STATE_DIR}" ) ]]; then
  echo "automation_state_directory_invalid=true" >&2
  exit 1
fi
install -d -o root -g root -m 0700 "${RUNTIME_STATE_DIR}"
if [[ -L "${UNIT_PATH}" || ( -e "${UNIT_PATH}" && ! -f "${UNIT_PATH}" ) ]]; then
  echo "automation_installed_unit_invalid=true" >&2
  exit 1
fi
if [[ -L "${CONFIG_PATH}" || ( -e "${CONFIG_PATH}" && ! -f "${CONFIG_PATH}" ) ]]; then
  echo "automation_installed_config_invalid=true" >&2
  exit 1
fi
if [[ -f "${UNIT_PATH}" && "${replace_unit}" -ne 1 ]] && ! cmp -s "${UNIT_SOURCE}" "${UNIT_PATH}"; then
  echo "automation_installed_unit_differs=true" >&2
  echo "automation_replace_unit_required=true" >&2
  exit 1
fi

rollback_dir="$(mktemp -d /run/autostop-manager-automation-install.XXXXXX)"
chmod 0700 "${rollback_dir}"
unit_existed=0
config_existed=0
service_was_active=0
service_was_enabled=0
rollback_armed=1
control_socket_was_live=0
if [[ -S "${SOCKET_PATH}" ]]; then
  control_socket_was_live=1
fi
if [[ -f "${UNIT_PATH}" ]]; then
  cp --archive --no-dereference "${UNIT_PATH}" "${rollback_dir}/unit"
  unit_existed=1
fi
if [[ -f "${CONFIG_PATH}" ]]; then
  cp --archive --no-dereference "${CONFIG_PATH}" "${rollback_dir}/config"
  config_existed=1
fi
if systemctl is-active --quiet "${UNIT_NAME}"; then
  service_was_active=1
fi
if systemctl is-enabled --quiet "${UNIT_NAME}"; then
  service_was_enabled=1
fi

finish_install() {
  exit_code=$?
  set +e
  if [[ -n "${temporary_config:-}" ]]; then
    rm -f -- "${temporary_config}"
  fi
  if [[ "${exit_code}" -ne 0 && "${rollback_armed}" -eq 1 ]]; then
    if [[ "${unit_existed}" -eq 1 ]]; then
      cp --archive --no-dereference "${rollback_dir}/unit" "${UNIT_PATH}"
    else
      rm -f -- "${UNIT_PATH}"
    fi
    if [[ "${config_existed}" -eq 1 ]]; then
      cp --archive --no-dereference "${rollback_dir}/config" "${CONFIG_PATH}"
    else
      rm -f -- "${CONFIG_PATH}"
    fi
    systemctl daemon-reload
    if [[ "${service_was_enabled}" -eq 1 ]]; then
      systemctl enable "${UNIT_NAME}"
    else
      systemctl disable "${UNIT_NAME}"
    fi
    if [[ "${service_was_active}" -eq 1 ]]; then
      systemctl restart "${UNIT_NAME}"
    else
      systemctl stop "${UNIT_NAME}"
    fi
    echo "manager_automation_installer_rollback_attempted=true" >&2
  fi
  rm -rf -- "${rollback_dir}"
  exit "${exit_code}"
}
trap finish_install EXIT

if [[ "${activate_under_hold}" -eq 1 ]]; then
  env \
    PYTHONPATH="${PROJECT_ROOT}" \
    PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUTOSTOP_MANAGER_ENV_FILE=/dev/null \
    AUTOSTOP_AUTOMATION_DB="${RUNTIME_DB}" \
    AUTOSTOP_AUTOMATION_CONTROL_SOCKET="${SOCKET_PATH}" \
    "${RUNTIME_PYTHON}" -m autostop_manager.automation_release hold \
      --release-attempt-key "${release_attempt_key}"
  if [[ "${control_socket_was_live}" -eq 0 ]]; then
    env \
      PYTHONPATH="${PROJECT_ROOT}" \
      PYTHONSAFEPATH=1 \
      PYTHONDONTWRITEBYTECODE=1 \
      AUTOSTOP_MANAGER_ENV_FILE=/dev/null \
      AUTOSTOP_AUTOMATION_DB="${RUNTIME_DB}" \
      AUTOSTOP_AUTOMATION_CONTROL_SOCKET="${SOCKET_PATH}" \
      "${RUNTIME_PYTHON}" -m autostop_manager.automation_release adopt-current \
        --release-attempt-key "${release_attempt_key}"
  fi
fi

allowed_uids="0,10001"
allowed_peers="0:codex|system,10001:crm_operator"
if telegram_uid="$(id -u autostop-work-telegram 2>/dev/null)"; then
  allowed_uids="${allowed_uids},${telegram_uid}"
  allowed_peers="${allowed_peers},${telegram_uid}:telegram_owner"
fi
install -d -o root -g root -m 0755 "${CONFIG_DIR}"
temporary_config="$(mktemp "${CONFIG_DIR}/.automation-control.env.XXXXXX")"
chmod 0600 "${temporary_config}"
printf 'AUTOSTOP_AUTOMATION_CONTROL_ALLOWED_UIDS=%s\n' "${allowed_uids}" >"${temporary_config}"
printf 'AUTOSTOP_AUTOMATION_CONTROL_PEERS=%s\n' "${allowed_peers}" >>"${temporary_config}"
printf 'AUTOSTOP_AUTOMATION_CONTROL_GID=10001\n' >>"${temporary_config}"
printf 'AUTOSTOP_MANAGER_REVISION=%s\n' "${manager_revision}" >>"${temporary_config}"
if [[ -n "${crm_revision}" ]]; then
  printf 'AUTOSTOP_AUTOMATION_CRM_REVISION=%s\n' "${crm_revision}" >>"${temporary_config}"
fi
if [[ -n "${crm_version}" ]]; then
  printf 'AUTOSTOP_AUTOMATION_CRM_VERSION=%s\n' "${crm_version}" >>"${temporary_config}"
fi
install -o root -g root -m 0600 "${temporary_config}" "${CONFIG_PATH}"
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNIT_PATH}"
fi

echo "manager_automation_unit_installed=true"
echo "activation_requested=$([[ "${activate}" -eq 1 ]] && echo true || echo false)"
echo "activation_under_hold=$([[ "${activate_under_hold}" -eq 1 ]] && echo true || echo false)"
if [[ "${activate}" -ne 1 ]]; then
  rollback_armed=0
  exit 0
fi

if systemctl is-active --quiet "${UNIT_NAME}"; then
  systemctl enable "${UNIT_NAME}"
  systemctl restart "${UNIT_NAME}"
else
  systemctl enable --now "${UNIT_NAME}"
fi

for _attempt in {1..50}; do
  systemctl is-active --quiet "${UNIT_NAME}" || exit 1
  [[ -S "${SOCKET_PATH}" ]] && break
  sleep 0.2
done
[[ -S "${SOCKET_PATH}" ]]
env \
  PYTHONPATH="${RELEASE_ROOT}" \
  PYTHONSAFEPATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  AUTOSTOP_AUTOMATION_CONTROL_SOCKET="${SOCKET_PATH}" \
  "${RUNTIME_PYTHON}" -m autostop_manager.automation_control status
if [[ "${activate_under_hold}" -eq 1 ]]; then
  env \
    PYTHONPATH="${RELEASE_ROOT}" \
    PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUTOSTOP_MANAGER_ENV_FILE=/dev/null \
    AUTOSTOP_AUTOMATION_CONTROL_SOCKET="${SOCKET_PATH}" \
    "${RUNTIME_PYTHON}" -m autostop_manager.automation_release seed \
      --release-attempt-key "${release_attempt_key}"
fi

echo "manager_automation_active=true"
echo "global_hold_release_required=$([[ "${activate_under_hold}" -eq 1 ]] && echo true || echo false)"
rollback_armed=0
