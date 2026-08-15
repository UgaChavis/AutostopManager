#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="autostop-telegram"
RUNTIME_DIR="/run/autostop-telegram"
PASSWORD_PATH="${RUNTIME_DIR}/2fa-password.once"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi

install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${RUNTIME_DIR}"
read -r -s -p "Telegram cloud password: " telegram_2fa_password
echo
if [[ -z "${telegram_2fa_password}" ]]; then
  echo "password_empty=true" >&2
  exit 1
fi

umask 077
printf '%s' "${telegram_2fa_password}" >"${PASSWORD_PATH}"
unset telegram_2fa_password
chown "${SERVICE_USER}:${SERVICE_USER}" "${PASSWORD_PATH}"
chmod 0600 "${PASSWORD_PATH}"
echo "telegram_2fa_password_ready=true"
