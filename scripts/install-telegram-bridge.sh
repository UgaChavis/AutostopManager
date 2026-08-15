#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ROOT="/opt/autostop-telegram-venv"
SERVICE_USER="autostop-telegram"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/autostop-telegram --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" /var/lib/autostop-telegram
install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" /etc/autostop-telegram

if [[ ! -x "${VENV_ROOT}/bin/python" ]]; then
  python3 -m venv "${VENV_ROOT}"
fi
"${VENV_ROOT}/bin/python" -m pip install --disable-pip-version-check \
  "Telethon>=1.40,<2" \
  "qrcode[pil]>=8,<9"
chmod -R a+rX "${VENV_ROOT}"

install -m 0644 "${PROJECT_ROOT}/deploy/systemd/autostop-telegram.service" /etc/systemd/system/
systemctl daemon-reload

echo "telegram_bridge_installed=true"
echo "credentials_present=$([[ -f /etc/autostop-telegram/credentials ]] && echo true || echo false)"
