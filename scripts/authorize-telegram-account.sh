#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi
if [[ $# -ne 2 || "$1" != "--account" ]]; then
  echo "usage: $0 --account personal|work" >&2
  exit 2
fi

account="$2"
case "${account}" in
  personal)
    service_unit="autostop-telegram.service"
    service_user="autostop-telegram"
    release_link="/opt/autostop-telegram-releases/current"
    venv_python="/opt/autostop-telegram-venv/bin/python"
    session_base="/var/lib/autostop-telegram/account.session"
    ;;
  work)
    service_unit="autostop-work-telegram.service"
    service_user="autostop-work-telegram"
    release_link="/opt/autostop-work-telegram-releases/current"
    venv_python="/opt/autostop-work-telegram-venv/bin/python"
    session_base="/var/lib/autostop-work-telegram/account.session"
    ;;
  *)
    echo "usage: $0 --account personal|work" >&2
    exit 2
    ;;
esac

if [[ ! -x "${venv_python}" || ! -d "${release_link}" ]]; then
  echo "${account}_release_unavailable=true" >&2
  exit 1
fi
if ! systemctl show --property=LoadState --value "${service_unit}" | grep -Fxq loaded; then
  echo "${account}_service_unavailable=true" >&2
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
      "${venv_python}" -m autostop_manager.telegram_bridge --account "${account}" probe \
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

session_files=(
  "${session_base}"
  "${session_base}-journal"
  "${session_base}-shm"
  "${session_base}-wal"
)

session_state_present() {
  local session_file
  for session_file in "${session_files[@]}"; do
    if [[ -e "${session_file}" || -L "${session_file}" ]]; then
      return 0
    fi
  done
  return 1
}

verify_private_session_files() {
  local session_file
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

if session_state_present && ! verify_private_session_files; then
  systemctl stop "${service_unit}" || true
  systemctl disable "${service_unit}" || true
  echo "${account}_session_permissions_invalid=true" >&2
  exit 1
fi

trap cleanup_failed_authorization EXIT

enable_and_verify_bridge() {
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
  echo "${account}_telegram_already_authorized=true"
  trap - EXIT
  exit 0
fi

# Only the selected daemon may be stopped for this bounded login.
if [[ "${was_active}" -eq 1 ]]; then
  systemctl stop "${service_unit}"
fi

login_output=""
login_status=0
login_output="$(
  sudo -u "${service_user}" sh -c 'umask 077; exec "$@"' sh \
    env PYTHONPATH="${release_link}" \
    "${venv_python}" -m autostop_manager.telegram_bridge --account "${account}" code-login
)" || login_status="$?"

if [[ "${login_status}" -ne 0 ]]; then
  if grep -Eq '"error": "account_already_authorized"' <<<"${login_output}"; then
    if ! verify_private_session_files || ! enable_and_verify_bridge; then
      echo "${account}_telegram_existing_authorization_unverified=true" >&2
      exit 1
    fi
    echo "${account}_telegram_already_authorized=true"
    trap - EXIT
    exit 0
  fi
  echo "${account}_telegram_login_failed=true" >&2
  exit 1
fi

if ! verify_private_session_files; then
  rm -f -- \
    "${session_base}" \
    "${session_base}-journal" \
    "${session_base}-shm" \
    "${session_base}-wal"
  echo "${account}_session_permissions_invalid=true" >&2
  exit 1
fi

if ! enable_and_verify_bridge; then
  echo "${account}_telegram_authorization_unverified=true" >&2
  exit 1
fi

echo "${account}_telegram_authorized=true"
trap - EXIT
exit 0
