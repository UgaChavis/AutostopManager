#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -ne 2 || "$1" != "--account" ]]; then
  echo "usage: $0 --account personal|work" >&2
  exit 2
fi
account="$2"

case "${account}" in
  personal)
    service_user="autostop-telegram"
    state_dir="/var/lib/autostop-telegram"
    config_dir="/etc/autostop-telegram"
    unit_source="${PROJECT_ROOT}/deploy/systemd/autostop-telegram.service"
    unit_path="/etc/systemd/system/autostop-telegram.service"
    venv_root="/opt/autostop-telegram-venv"
    ;;
  work)
    service_user="autostop-work-telegram"
    state_dir="/var/lib/autostop-work-telegram"
    config_dir="/etc/autostop-work-telegram"
    unit_source="${PROJECT_ROOT}/deploy/systemd/autostop-work-telegram.service"
    unit_path="/etc/systemd/system/autostop-work-telegram.service"
    venv_root="/opt/autostop-work-telegram-venv"
    ;;
  *)
    echo "account_invalid=true" >&2
    exit 2
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi

if ! id "${service_user}" >/dev/null 2>&1; then
  useradd --system --home-dir "${state_dir}" --shell /usr/sbin/nologin "${service_user}"
fi

install -d -m 0700 -o "${service_user}" -g "${service_user}" "${state_dir}"
if [[ "${account}" == "work" ]]; then
  if [[ -L "${config_dir}" || ( -e "${config_dir}" && ! -d "${config_dir}" ) ]]; then
    echo "work_config_directory_invalid=true" >&2
    exit 1
  fi
  install -d -m 0710 -o root -g "${service_user}" "${config_dir}"
  if [[ "$(stat -c '%U:%G:%a' "${config_dir}")" != "root:${service_user}:710" ]]; then
    echo "work_config_directory_permissions_invalid=true" >&2
    exit 1
  fi
else
  install -d -m 0700 -o "${service_user}" -g "${service_user}" "${config_dir}"
fi

if [[ "${account}" == "work" ]]; then
  target_credentials="${config_dir}/credentials"
  if [[ -L "${target_credentials}" || ( -e "${target_credentials}" && ! -f "${target_credentials}" ) ]]; then
    echo "work_credentials_invalid=true" >&2
    exit 1
  fi
  if [[ ! -e "${target_credentials}" ]]; then
    source_credentials="/etc/autostop-telegram/credentials"
    if [[ ! -f "${source_credentials}" || -L "${source_credentials}" ]]; then
      echo "source_credentials_present=false" >&2
      exit 1
    fi
    if [[ "$(stat -c '%U:%G:%a' "${source_credentials}")" != "autostop-telegram:autostop-telegram:600" ]]; then
      echo "source_credentials_permissions_invalid=true" >&2
      exit 1
    fi
    temporary_credentials="$(mktemp "${config_dir}/.credentials.XXXXXX")"
    if ! cp --no-dereference -- "${source_credentials}" "${temporary_credentials}"; then
      rm -f -- "${temporary_credentials}"
      echo "work_credentials_copy_failed=true" >&2
      exit 1
    fi
    if [[ -L "${temporary_credentials}" || ! -f "${temporary_credentials}" ]]; then
      rm -f -- "${temporary_credentials}"
      echo "work_credentials_copy_invalid=true" >&2
      exit 1
    fi
    chown "${service_user}:${service_user}" "${temporary_credentials}"
    chmod 0600 "${temporary_credentials}"
    mv -T -- "${temporary_credentials}" "${target_credentials}"
  fi
fi

if [[ -f "${config_dir}/credentials" ]] \
  && [[ "$(stat -c '%U:%G:%a' "${config_dir}/credentials")" != "${service_user}:${service_user}:600" ]]; then
  echo "credentials_permissions_invalid=true" >&2
  exit 1
fi

if [[ ! -x "${venv_root}/bin/python" ]]; then
  python3 -m venv "${venv_root}"
fi
"${venv_root}/bin/python" -m pip install --disable-pip-version-check \
  "Telethon>=1.40,<2" \
  "qrcode[pil]>=8,<9"
if [[ "${account}" == "work" ]]; then
  chown -R root:root "${venv_root}"
  chmod -R go-w "${venv_root}"
fi
chmod -R a+rX "${venv_root}"

install -m 0644 "${unit_source}" "${unit_path}"
systemctl daemon-reload

echo "telegram_bridge_installed=true"
echo "account=${account}"
echo "credentials_present=$([[ -f "${config_dir}/credentials" ]] && echo true || echo false)"
