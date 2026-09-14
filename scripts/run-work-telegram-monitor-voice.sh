#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo 'monitor_voice_root_required=true' >&2
  exit 1
fi

release_root='/opt/autostop-work-telegram-releases/current'
venv_python='/opt/autostop-work-telegram-venv/bin/python'
release_dir="$(readlink -f -- "${release_root}" 2>/dev/null || true)"

[[ "${release_dir}" == /opt/autostop-work-telegram-releases/* && -d "${release_dir}" && ! -L "${release_dir}" \
  && -x "${venv_python}" ]] || {
  echo 'monitor_voice_runtime_unavailable=true' >&2
  exit 1
}

exec /usr/bin/env \
  PYTHONDONTWRITEBYTECODE=1 \
  "PYTHONPATH=${release_dir}" \
  "${venv_python}" \
  -m autostop_manager.telegram_monitor_voice "$@"
