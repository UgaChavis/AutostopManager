#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 || "$1" != "--account" ]]; then
  echo "usage: $0 --account personal|work" >&2
  exit 2
fi
account="$2"

case "${account}" in
  personal)
    service_user="autostop-telegram"
    runtime_dir="/run/autostop-telegram"
    ;;
  work)
    service_user="autostop-work-telegram"
    runtime_dir="/run/autostop-work-telegram"
    ;;
  *)
    echo "account_invalid=true" >&2
    exit 2
    ;;
esac

PASSWORD_PATH="${runtime_dir}/2fa-password.once"
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

install -d -m 0700 -o "${service_user}" -g "${service_user}" "${runtime_dir}"
read -r -s -p "Telegram cloud password: " telegram_2fa_password
echo
if [[ -z "${telegram_2fa_password}" ]]; then
  echo "password_empty=true" >&2
  exit 1
fi

umask 077
if [[ "$(stat -c '%d' /run)" != "$(stat -c '%d' "${runtime_dir}")" ]]; then
  echo "runtime_filesystem_invalid=true" >&2
  exit 1
fi
PASSWORD_TMP="$(mktemp --tmpdir=/run autostop-telegram-2fa-password.XXXXXX)"
trap cleanup_password_tmp EXIT HUP INT TERM
printf '%s' "${telegram_2fa_password}" >"${PASSWORD_TMP}"
unset telegram_2fa_password
chown "${service_user}:${service_user}" "${PASSWORD_TMP}"
chmod 0600 "${PASSWORD_TMP}"

# The service user owns the runtime directory, so never write or chown a predictable
# pathname there.  A same-filesystem rename replaces a hostile symlink instead
# of following it.
mv -T -- "${PASSWORD_TMP}" "${PASSWORD_PATH}"
PASSWORD_TMP=""
trap - EXIT HUP INT TERM
echo "telegram_2fa_password_ready=true"
echo "account=${account}"
