#!/usr/bin/env bash
set -Eeuo pipefail

service_unit="autostop-work-telegram.service"
service_user="autostop-work-telegram"
release_link="/opt/autostop-work-telegram-releases/current"
venv_python="/opt/autostop-work-telegram-venv/bin/python"
monitor_env="/etc/autostop-work-telegram/monitor.env"
owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"
control_lock="/run/autostop-work-telegram-control.lock"
pending_monitor_env=""
wake_unit="autostop-codex-wake.service"
wake_config="/etc/autostop-work-telegram/wake.json"
wake_python="/opt/AutostopManager/.venv/bin/python"
export PYTHONSAFEPATH=1

usage() { echo "usage: $0 --enable|--disable|--status" >&2; }
if [[ "${EUID}" -ne 0 ]]; then echo "run_as_root_required=true" >&2; exit 1; fi
if [[ $# -ne 1 ]]; then usage; exit 2; fi
exec 9>"${control_lock}"
flock -x 9

if [[ -e "${monitor_env}" || -L "${monitor_env}" ]] \
  && { [[ ! -f "${monitor_env}" || -L "${monitor_env}" ]] \
    || [[ "$(stat -c '%U:%G:%a' "${monitor_env}")" != "root:root:644" ]]; }; then
  echo "work_telegram_monitor_config_invalid=true" >&2
  exit 1
fi
if [[ -e "${owner_notification_env}" || -L "${owner_notification_env}" ]] \
  && { [[ ! -f "${owner_notification_env}" || -L "${owner_notification_env}" ]] \
    || [[ "$(stat -c '%U:%G:%a' "${owner_notification_env}")" != "root:root:600" ]]; }; then
  echo "work_telegram_owner_notification_config_invalid=true" >&2
  exit 1
fi

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

bridge_status() {
  sudo -u "${service_user}" env PYTHONPATH="${release_link}" \
    "${venv_python}" -m autostop_manager.telegram_bridge --account work status
}

wait_for_outbound_only() {
  local status
  for _attempt in {1..15}; do
    if systemctl is-active --quiet "${service_unit}" \
      && status="$(bridge_status)" \
      && grep -Eq '"transport_ready":[[:space:]]*true' <<<"${status}" \
      && grep -Eq '"inbound_enabled":[[:space:]]*false' <<<"${status}"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

disable_duty() {
  local workers load_state
  if [[ -f "${wake_config}" ]]; then
    if systemctl is-active --quiet "${wake_unit}"; then
      PYTHONPATH="${release_link}" "${wake_python}" -m autostop_manager.telegram_wake pause || return 1
    fi
    systemctl disable --now "${wake_unit}" || return 1
  fi
  [[ ! -e "${monitor_env}" && ! -L "${monitor_env}" ]] || unlink -- "${monitor_env}" || return 1
  load_state="$(systemctl show --property=LoadState --value "${service_unit}" 2>/dev/null || true)"
  if [[ "${load_state}" == "loaded" ]]; then
    # Duty controls inbound event handling only. The bridge remains enabled so
    # guarded scheduled notifications can still use its local outbound RPC.
    systemctl enable "${service_unit}" || return 1
    systemctl restart "${service_unit}" || return 1
    wait_for_outbound_only || return 1
  elif [[ "${load_state}" != "not-found" ]]; then
    return 1
  fi
  stop_active_media_workers || return 1
  workers="$(active_media_workers)" || return 1
  [[ -z "${workers}" ]] || return 1
}

enable_duty() {
  local monitor_env_dir monitor_status release_dir intent_changed=0 wake_ready=0
  release_dir="$(readlink -f -- "${release_link}" 2>/dev/null || true)"
  [[ -L "${release_link}" && "${release_dir}" == /opt/autostop-work-telegram-releases/* && -d "${release_dir}" && ! -L "${release_dir}" && -x "${venv_python}" ]] || return 1
  monitor_env_dir="$(dirname -- "${monitor_env}")"
  [[ -d "${monitor_env_dir}" && ! -L "${monitor_env_dir}" ]] || return 1
  pending_monitor_env="$(mktemp "${monitor_env_dir}/.monitor.env.XXXXXX")" || return 1
  printf '%s\n' 'AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1' > "${pending_monitor_env}" || return 1
  if [[ -f "${wake_config}" ]]; then
    printf '%s\n' 'AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET=/run/autostop-codex-wake/wake.sock' >> "${pending_monitor_env}" || return 1
    systemctl start autostop-codex-start.service || return 1
    systemctl enable --now "${wake_unit}" || return 1
    for _attempt in {1..15}; do
      if PYTHONPATH="${release_link}" "${wake_python}" -m autostop_manager.telegram_wake status \
        | "${venv_python}" -c 'import json,sys; s=json.load(sys.stdin); sys.exit(not(s.get("enabled") and s.get("connected")))'; then
        wake_ready=1
        break
      fi
      sleep 1
    done
    [[ "${wake_ready}" -eq 1 ]] || return 1
  fi
  chown root:root "${pending_monitor_env}" || return 1
  chmod 0644 "${pending_monitor_env}" || return 1
  if [[ ! -f "${monitor_env}" ]] || ! cmp -s "${pending_monitor_env}" "${monitor_env}"; then
    intent_changed=1
  fi
  mv -T -- "${pending_monitor_env}" "${monitor_env}" || return 1
  pending_monitor_env=""
  if systemctl is-active --quiet "${service_unit}"; then
    systemctl enable "${service_unit}" || return 1
    if [[ "${intent_changed}" -eq 1 ]]; then
      systemctl restart "${service_unit}" || return 1
    fi
  else
    systemctl enable --now "${service_unit}" || return 1
  fi
  # Type=simple becomes active before the bridge connects and opens its socket.
  for _attempt in {1..15}; do
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

duty_is_paused() {
  local value
  [[ ! -e "${monitor_env}" && ! -L "${monitor_env}" ]] || return 1
  value="$(systemctl show --property=LoadState --value "${wake_unit}")" || return 1
  [[ "${value}" == "loaded" ]] || return 1
  value="$(systemctl show --property=ActiveState --value "${wake_unit}")" || return 1
  [[ "${value}" == "inactive" ]] || return 1
  value="$(systemctl show --property=UnitFileState --value "${wake_unit}")" || return 1
  [[ "${value}" == "disabled" ]] || return 1
  systemctl is-active --quiet "${service_unit}" || return 1
  value="$(systemctl show --property=UnitFileState --value "${service_unit}")" || return 1
  [[ "${value}" == "enabled" ]] || return 1
  wait_for_outbound_only
}

case "$1" in
  --status)
    bridge_state="$(bridge_status)" || { printf '%s\n' '{"ok":false,"transport_ready":false,"error":"bridge_unavailable"}'; exit 1; }
    wake_active=false
    if [[ -f "${wake_config}" ]] && systemctl is-active --quiet "${wake_unit}"; then wake_active=true; fi
    BRIDGE_STATE="${bridge_state}" WAKE_ACTIVE="${wake_active}" "${venv_python}" -c '
import json, os
s=json.loads(os.environ["BRIDGE_STATE"])
inbound=bool(s.get("inbound_enabled"))
print(json.dumps({
  "ok": bool(s.get("ok")),
  "transport_ready": bool(s.get("transport_ready")),
  "inbound_enabled": inbound,
  "owner_notification_configured": bool(s.get("owner_notification_configured")),
  "wake_active": os.environ["WAKE_ACTIVE"] == "true",
  "state": "inbound_enabled" if inbound else "outbound_only",
  "polling": False,
}, separators=(",", ":"), sort_keys=True))'
    ;;
  --disable)
    trap cleanup_incomplete_duty EXIT
    disable_duty || { echo "work_telegram_duty_disable_failed=true" >&2; exit 1; }
    trap - EXIT
    printf '%s\n' "work_telegram_duty=disabled" "monitoring=off" "outbound=ready"
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
