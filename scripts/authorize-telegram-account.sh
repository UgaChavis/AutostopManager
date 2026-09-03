#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi
if [[ $# -ne 2 || "$1" != "--account" || "$2" != "work" ]]; then
  echo "usage: $0 --account work" >&2
  exit 2
fi

service_unit="autostop-work-telegram.service"
service_user="autostop-work-telegram"
release_link="/opt/autostop-work-telegram-releases/current"
venv_python="/opt/autostop-work-telegram-venv/bin/python"
session_base="/var/lib/autostop-work-telegram/account.session"

if [[ ! -x "${venv_python}" || ! -d "${release_link}" ]]; then
  echo "work_release_unavailable=true" >&2
  exit 1
fi
if ! systemctl show --property=LoadState --value "${service_unit}" | grep -Fxq loaded; then
  echo "work_service_unavailable=true" >&2
  exit 1
fi

was_active=0
if systemctl is-active --quiet "${service_unit}"; then
  was_active=1
fi
was_enabled=0
if systemctl is-enabled --quiet "${service_unit}"; then
  was_enabled=1
fi

bridge_authorized() {
  systemctl is-active --quiet "${service_unit}" \
    && sudo -u "${service_user}" env PYTHONPATH="${release_link}" \
      "${venv_python}" -m autostop_manager.telegram_bridge --account work probe \
      | grep -Eq '"authorized": true'
}

# shellcheck disable=SC2317  # Called only by the EXIT-trap cleanup.
restore_original_service_state() {
  systemctl stop "${service_unit}" || true
  if [[ "${was_enabled}" -eq 1 ]]; then
    systemctl enable "${service_unit}" || true
  else
    systemctl disable "${service_unit}" || true
  fi
  if [[ "${was_active}" -eq 1 ]]; then
    systemctl start "${service_unit}" || true
  fi
}

# shellcheck disable=SC2317  # Invoked by the EXIT trap.
cleanup_failed_authorization() {
  authorization_exit_code="$?"
  if [[ "${authorization_exit_code}" -ne 0 ]]; then
    restore_original_service_state
  fi
  return "${authorization_exit_code}"
}
trap cleanup_failed_authorization EXIT

verify_private_session_files() {
  local session_file
  local session_files=(
    "${session_base}"
    "${session_base}-journal"
    "${session_base}-shm"
    "${session_base}-wal"
  )
  if [[ -L "${session_base}" || ! -f "${session_base}" ]]; then
    return 1
  fi
  for session_file in "${session_files[@]}"; do
    if [[ ! -e "${session_file}" && ! -L "${session_file}" ]]; then
      continue
    fi
    if [[ -L "${session_file}" || ! -f "${session_file}" \
      || "$(stat -c '%U:%G:%a' "${session_file}")" != "${service_user}:${service_user}:600" ]]; then
      return 1
    fi
  done
}

enable_and_verify_work_bridge() {
  systemctl enable --now "${service_unit}"
  for _attempt in $(seq 1 15); do
    if bridge_authorized; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if bridge_authorized; then
  systemctl enable "${service_unit}"
  echo "work_telegram_already_authorized=true"
  trap - EXIT
  exit 0
fi

# Only the isolated work daemon may be stopped for this bounded login; the
# existing personal bridge is never referenced by this script.
if [[ "${was_active}" -eq 1 ]]; then
  systemctl stop "${service_unit}"
fi

login_output=""
login_status=0
login_output="$(
  sudo -u "${service_user}" env PYTHONPATH="${release_link}" \
    "${venv_python}" -m autostop_manager.telegram_bridge --account work code-login
)" || login_status="$?"

if [[ "${login_status}" -ne 0 ]]; then
  if grep -Eq '"error": "account_already_authorized"' <<<"${login_output}"; then
    if ! verify_private_session_files || ! enable_and_verify_work_bridge; then
      echo "work_telegram_existing_authorization_unverified=true" >&2
      exit 1
    fi
    echo "work_telegram_already_authorized=true"
    trap - EXIT
    exit 0
  fi
  echo "work_telegram_login_failed=true" >&2
  exit 1
fi

if ! verify_private_session_files; then
  rm -f -- \
    "${session_base}" \
    "${session_base}-journal" \
    "${session_base}-shm" \
    "${session_base}-wal"
  echo "work_session_permissions_invalid=true" >&2
  exit 1
fi

if ! enable_and_verify_work_bridge; then
  echo "work_telegram_authorization_unverified=true" >&2
  exit 1
fi

echo "work_telegram_authorized=true"
trap - EXIT
exit 0
