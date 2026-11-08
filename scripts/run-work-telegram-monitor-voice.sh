#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo 'monitor_voice_root_required=true' >&2
  exit 1
fi

release_root='/opt/autostop-work-telegram-releases/current'
release_dir="$(readlink -f -- "${release_root}" 2>/dev/null || true)"

[[ ( "${release_dir}" == /opt/autostop-work-telegram-releases/* \
  || "${release_dir}" == /opt/autostop-manager-releases/* ) && -d "${release_dir}" && ! -L "${release_dir}" ]] || {
  echo 'monitor_voice_runtime_unavailable=true' >&2
  exit 1
}

script_path="$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || true)"
[[ "$(dirname -- "${script_path}")" == "${release_dir}/scripts" ]] || {
  echo 'monitor_voice_release_mismatch=true' >&2
  exit 1
}

# Self-check is also used during deployment before the bridge is running.
if [[ $# -ne 1 || "${1:-}" != '--self-check' ]]; then
  bridge_pid="$(timeout 10 systemctl show --property=MainPID --value autostop-work-telegram.service 2>/dev/null || true)"
  [[ "${bridge_pid}" =~ ^[1-9][0-9]*$ ]] || { echo 'monitor_voice_release_mismatch=true' >&2; exit 1; }
  bridge_root="$(readlink -f -- "/proc/${bridge_pid}/cwd" 2>/dev/null || true)"
  [[ "${bridge_root}" == "${release_dir}" ]] || { echo 'monitor_voice_release_mismatch=true' >&2; exit 1; }
fi

venv_python="$(bash "${release_dir}/scripts/work-telegram-python.sh" bridge)" || exit 1

exec /usr/bin/env \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONSAFEPATH=1 \
  "PYTHONPATH=${release_dir}" \
  "${venv_python}" \
  -m autostop_manager.telegram_monitor_voice "$@"
