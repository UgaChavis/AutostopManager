#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="autostop-telegram"
RUNTIME_DIR="/run/autostop-telegram"
PASSWORD_PATH="${RUNTIME_DIR}/2fa-password.once"
PASSWORD_TMP=""

cleanup_password_tmp() {
  if [[ -n "${PASSWORD_TMP}" ]]; then
    rm -f -- "${PASSWORD_TMP}" || true
  fi
}

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
if [[ "$(stat -c '%d' /run)" != "$(stat -c '%d' "${RUNTIME_DIR}")" ]]; then
  echo "runtime_filesystem_invalid=true" >&2
  exit 1
fi
PASSWORD_TMP="$(mktemp --tmpdir=/run autostop-telegram-2fa-password.XXXXXX)"
trap cleanup_password_tmp EXIT HUP INT TERM
printf '%s' "${telegram_2fa_password}" >"${PASSWORD_TMP}"
unset telegram_2fa_password
chown "${SERVICE_USER}:${SERVICE_USER}" "${PASSWORD_TMP}"
chmod 0600 "${PASSWORD_TMP}"

# The service user owns RUNTIME_DIR, so never write or chown a predictable
# pathname there.  A same-filesystem rename replaces a hostile symlink instead
# of following it.
mv -T -- "${PASSWORD_TMP}" "${PASSWORD_PATH}"
PASSWORD_TMP=""
trap - EXIT HUP INT TERM
echo "telegram_2fa_password_ready=true"
