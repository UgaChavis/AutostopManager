#!/usr/bin/env bash
set -Eeuo pipefail

service_unit="autostop-work-telegram.service"
service_user="autostop-work-telegram"
release_link="/opt/autostop-work-telegram-releases/current"
venv_python="/opt/autostop-work-telegram-venv/bin/python"
monitor_env="/etc/autostop-work-telegram/monitor.env"
control_lock="/run/autostop-work-telegram-control.lock"
pending_monitor_env=""

usage() { echo "usage: $0 --enable|--disable" >&2; }
if [[ "${EUID}" -ne 0 ]]; then echo "run_as_root_required=true" >&2; exit 1; fi
if [[ $# -ne 1 ]]; then usage; exit 2; fi
exec 9>"${control_lock}"
flock -x 9

active_media_workers() {
  systemctl list-units --type=service --all --no-legend --no-pager \
    'autostop-work-telegram-media-*' | awk '$3 ~ /^(active|activating|deactivating)$/ { print $1 }'
}

stop_active_media_workers() {
  local workers
  workers="$(active_media_workers)" || return 1
  [[ -z "${workers}" ]] && return 0
  local -a units=()
  mapfile -t units <<<"${workers}"
  systemctl stop "${units[@]}" || return 1
}

disable_duty() {
  local workers load_state
  [[ ! -e "${monitor_env}" && ! -L "${monitor_env}" ]] || unlink -- "${monitor_env}" || return 1
  load_state="$(systemctl show --property=LoadState --value "${service_unit}" 2>/dev/null || true)"
  if [[ "${load_state}" == "loaded" ]]; then
    systemctl disable "${service_unit}" || return 1
    systemctl stop "${service_unit}" || return 1
    [[ "$(systemctl show --property=ActiveState --value "${service_unit}")" == "inactive" ]] || return 1
    [[ "$(systemctl show --property=UnitFileState --value "${service_unit}")" == "disabled" ]] || return 1
  elif [[ "${load_state}" != "not-found" ]]; then
    return 1
  fi
  stop_active_media_workers || return 1
  workers="$(active_media_workers)" || return 1
  [[ -z "${workers}" ]] || return 1
}

enable_duty() {
  local monitor_env_dir monitor_status release_dir attempt
  release_dir="$(readlink -f -- "${release_link}" 2>/dev/null || true)"
  [[ -L "${release_link}" && "${release_dir}" == /opt/autostop-work-telegram-releases/* && -d "${release_dir}" && ! -L "${release_dir}" && -x "${venv_python}" ]] || return 1
  monitor_env_dir="$(dirname -- "${monitor_env}")"
  [[ -d "${monitor_env_dir}" && ! -L "${monitor_env_dir}" ]] || return 1
  pending_monitor_env="$(mktemp "${monitor_env_dir}/.monitor.env.XXXXXX")" || return 1
  printf '%s\n' 'AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1' > "${pending_monitor_env}" || return 1
  chown root:root "${pending_monitor_env}" || return 1
  chmod 0644 "${pending_monitor_env}" || return 1
  mv -T -- "${pending_monitor_env}" "${monitor_env}" || return 1
  pending_monitor_env=""
  if systemctl is-active --quiet "${service_unit}"; then
    systemctl enable "${service_unit}" || return 1
    systemctl restart "${service_unit}" || return 1
  else
    systemctl enable --now "${service_unit}" || return 1
  fi
  # Type=simple becomes active before the bridge connects and opens its socket.
  for attempt in {1..15}; do
    if systemctl is-active --quiet "${service_unit}" \
      && monitor_status="$(sudo -u "${service_user}" env PYTHONPATH="${release_link}" "${venv_python}" -m autostop_manager.telegram_bridge --account work monitor-status)" \
      && grep -Eq '"enabled": true' <<<"${monitor_status}" \
      && grep -Eq '"retention": "memory_only"' <<<"${monitor_status}"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cleanup_incomplete_duty() {
  local exit_code="$?"
  if [[ -n "${pending_monitor_env}" && -f "${pending_monitor_env}" && ! -L "${pending_monitor_env}" ]]; then unlink -- "${pending_monitor_env}" || true; fi
  disable_duty || true
  return "${exit_code}"
}

case "$1" in
  --disable)
    trap cleanup_incomplete_duty EXIT
    disable_duty || { echo "work_telegram_duty_disable_failed=true" >&2; exit 1; }
    trap - EXIT
    printf '%s\n' "work_telegram_duty=disabled" "monitoring=off"
    ;;
  --enable)
    trap cleanup_incomplete_duty EXIT
    enable_duty || { echo "work_telegram_duty_enable_failed=true" >&2; exit 1; }
    trap - EXIT
    printf '%s\n' "work_telegram_duty=enabled" "monitoring=enabled" "retention=memory_only"
    ;;
  *)
    usage; exit 2
    ;;
esac
