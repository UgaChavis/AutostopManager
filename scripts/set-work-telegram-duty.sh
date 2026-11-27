#!/usr/bin/env bash
set -Eeuo pipefail

service_unit="autostop-work-telegram.service"
service_user="autostop-work-telegram"
release_link="/opt/autostop-work-telegram-releases/current"
venv_python=""
monitor_env="/etc/autostop-work-telegram/monitor.env"
control_lock="/run/autostop-work-telegram-control.lock"
pending_monitor_env=""
preserve_existing_duty=0
published_duty_intent=0
wake_unit="autostop-codex-wake.service"
wake_config="/etc/autostop-work-telegram/wake.json"
wake_python=""
wake_activation_attempted=0
wake_was_active=0
wake_was_enabled=0
export PYTHONSAFEPATH=1

usage() { echo "usage: $0 --enable|--disable|--status [--expected-release-dir DIR]" >&2; }
if [[ "${EUID}" -ne 0 ]]; then echo "run_as_root_required=true" >&2; exit 1; fi
if [[ $# -ne 1 && $# -ne 3 ]]; then usage; exit 2; fi
operation="$1"
expected_release_dir=""
if [[ $# -eq 3 ]]; then
  [[ "$2" == "--expected-release-dir" ]] || { usage; exit 2; }
  expected_release_dir="$3"
  [[ -n "${expected_release_dir}" ]] || { usage; exit 2; }
fi
if [[ "${operation}" == "--status" ]]; then
  if [[ ! -f "${control_lock}" || -L "${control_lock}" ]] || ! { exec 9<"${control_lock}"; } 2>/dev/null; then
    printf '%s\n' '{"ok":false,"error":"duty_control_lock_unavailable"}'
    exit 1
  fi
  if ! flock -xn 9; then
    printf '%s\n' '{"ok":false,"error":"duty_control_busy"}'
    exit 1
  fi
else
  exec 9>"${control_lock}"
  flock -x 9
fi
if [[ -n "${expected_release_dir}" ]]; then
  current_release_dir="$(readlink -f -- "${release_link}" 2>/dev/null || true)"
  if [[ ! -L "${release_link}" || ( "${expected_release_dir}" != "$(dirname -- "${release_link}")/"* \
    && "${expected_release_dir}" != /opt/autostop-manager-releases/* ) \
    || ! -d "${expected_release_dir}" || -L "${expected_release_dir}" \
    || "${current_release_dir}" != "${expected_release_dir}" ]]; then
    echo "work_telegram_release_target_changed=true" >&2
    exit 1
  fi
fi

if [[ -e "${monitor_env}" || -L "${monitor_env}" ]] \
  && { [[ ! -f "${monitor_env}" || -L "${monitor_env}" ]] \
    || [[ "$(stat -c '%U:%G:%a' "${monitor_env}")" != "root:root:644" ]]; }; then
  echo "work_telegram_monitor_config_invalid=true" >&2
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

load_bridge_python() {
  venv_python="${venv_python:-$(bash "${release_link}/scripts/work-telegram-python.sh" bridge)}"
}

wake_control() {
  wake_python="${wake_python:-$(bash "${release_link}/scripts/work-telegram-python.sh" wake)}" || return 1
  PYTHONPATH="${release_link}" "${wake_python}" -m autostop_manager.telegram_wake "$@"
}

bridge_status() {
  load_bridge_python || return 1
  sudo -u "${service_user}" env PYTHONSAFEPATH=1 PYTHONPATH="${release_link}" \
    "${venv_python}" -m autostop_manager.telegram_bridge --account work status
}

durable_release() {
  [[ -f "${release_link}/autostop_manager/telegram_inbox.py" ]]
}

durable_duty_cutoffs() {
  durable_release && grep -Fxq 'WORK_DUTY_CUTOFF_DIRECTORY_SUFFIX = ".off.d"' \
    "${release_link}/autostop_manager/telegram_bridge.py" 2>/dev/null
}

valid_duty_off_directory() {
  [[ -d "$1" && ! -L "$1" && "$(stat -c '%U:%G:%a' "$1")" == "root:root:755" ]]
}

duty_off_markers() {
  local -n result="$1"
  local marker stamp directory="${monitor_env}.off.d"
  [[ -e "${directory}" || -L "${directory}" ]] || return 0
  valid_duty_off_directory "${directory}" || return 1
  for marker in "${directory}"/*; do
    [[ -e "${marker}" || -L "${marker}" ]] || continue
    stamp="${marker##*/}"
    if [[ ! "${stamp}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$ \
      || ! -f "${marker}" || -L "${marker}" \
      || "$(stat -c '%U:%G:%a' "${marker}")" != "root:root:644" ]]; then
      return 1
    fi
    result+=("${marker}")
  done
}

prune_duty_off_markers() {
  local marker
  for marker in "$@"; do
    unlink -- "${marker}" || return 1
  done
}

withdraw_duty_intent() {
  local stamp marker directory="${monitor_env}.off.d"
  [[ -e "${monitor_env}" || -L "${monitor_env}" ]] || return 0
  if ! durable_duty_cutoffs; then
    unlink -- "${monitor_env}"
    return
  fi
  if [[ ! -e "${directory}" && ! -L "${directory}" ]]; then
    install -d -o root -g root -m 0755 -- "${directory}" || return 1
  fi
  valid_duty_off_directory "${directory}" || return 1
  stamp="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)" || return 1
  marker="${directory}/${stamp}"
  [[ ! -e "${marker}" && ! -L "${marker}" ]] || return 1
  mv -T -- "${monitor_env}" "${marker}"
}

bridge_control() {
  load_bridge_python || return 1
  PYTHONPATH="${release_link}" "${venv_python}" -m autostop_manager.telegram_bridge --account work "$@"
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
  local workers load_state stop_before_withdrawal=0 outbound_ready=0 manage_wake="${1:-1}"
  local -a off_markers=()
  if durable_duty_cutoffs; then
    if [[ -f "${monitor_env}" ]]; then stop_before_withdrawal=1; fi
    if systemctl is-active --quiet "${service_unit}" && ! bridge_control duty-set --disabled >/dev/null; then
      [[ "${stop_before_withdrawal}" -eq 1 ]] || return 1
    fi
  elif durable_release && systemctl is-active --quiet "${service_unit}"; then
    bridge_control duty-set --disabled >/dev/null || return 1
  fi
  if [[ "${manage_wake}" -eq 1 && -f "${wake_config}" ]]; then
    if systemctl is-active --quiet "${wake_unit}"; then
      wake_control pause || return 1
    fi
    systemctl disable --now "${wake_unit}" || return 1
  fi
  if [[ "${stop_before_withdrawal}" -eq 1 ]]; then
    load_state="$(systemctl show --property=LoadState --value "${service_unit}" 2>/dev/null)" || return 1
    case "${load_state}" in
      loaded) systemctl stop "${service_unit}" || return 1 ;;
      not-found) ;;
      *) return 1 ;;
    esac
  fi
  withdraw_duty_intent || return 1
  if durable_duty_cutoffs; then
    duty_off_markers off_markers || return 1
  fi
  load_state="$(systemctl show --property=LoadState --value "${service_unit}" 2>/dev/null || true)"
  if [[ "${load_state}" == "loaded" ]]; then
    # Duty controls inbound event handling only. The bridge remains enabled so
    # guarded scheduled notifications can still use its local outbound RPC.
    systemctl enable "${service_unit}" || return 1
    systemctl restart "${service_unit}" || return 1
    wait_for_outbound_only || return 1
    outbound_ready=1
  elif [[ "${load_state}" != "not-found" ]]; then
    return 1
  fi
  stop_active_media_workers || return 1
  workers="$(active_media_workers)" || return 1
  [[ -z "${workers}" ]] || return 1
  if [[ "${outbound_ready}" -eq 1 ]]; then prune_duty_off_markers "${off_markers[@]}" || return 1; fi
}

enable_duty() {
  local monitor_env_dir monitor_status release_dir duty_requested_at="" intent_changed=0 wake_ready=0
  local -a off_markers=()
  load_bridge_python || return 1
  release_dir="$(readlink -f -- "${release_link}" 2>/dev/null || true)"
  [[ -L "${release_link}" && ( "${release_dir}" == /opt/autostop-work-telegram-releases/* \
    || "${release_dir}" == /opt/autostop-manager-releases/* ) && -d "${release_dir}" && ! -L "${release_dir}" && -x "${venv_python}" ]] || return 1
  monitor_env_dir="$(dirname -- "${monitor_env}")"
  [[ -d "${monitor_env_dir}" && ! -L "${monitor_env_dir}" ]] || return 1
  pending_monitor_env="$(mktemp "${monitor_env_dir}/.monitor.env.XXXXXX")" || return 1
  printf '%s\n' 'AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1' > "${pending_monitor_env}" || return 1
  if [[ -f "${wake_config}" ]]; then
    printf '%s\n' 'AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET=/run/autostop-codex-wake/wake.sock' >> "${pending_monitor_env}" || return 1
    systemctl start autostop-codex-start.service || return 1
    if durable_duty_cutoffs && [[ "${preserve_existing_duty}" -eq 0 ]]; then
      if systemctl is-active --quiet "${wake_unit}"; then wake_was_active=1; fi
      if systemctl is-enabled --quiet "${wake_unit}"; then wake_was_enabled=1; fi
      wake_activation_attempted=1
    fi
    systemctl enable --now "${wake_unit}" || return 1
    for _attempt in {1..15}; do
      if durable_release; then
        # Intake and execution are independent. A held job must not prevent
        # intake being enabled, and enable must never erase the held outcome.
        wake_ready=1
        break
      fi
      if wake_control status \
        | "${venv_python}" -c '
import json,sys
s=json.load(sys.stdin)
recovering=s.get("recovery_state") in {"starting", "reconnecting", "reconciling"}
ready=s.get("ready", s.get("connected", False))
sys.exit(not(s.get("ok") and s.get("enabled") and (ready or recovering)))'; then
        wake_ready=1
        break
      fi
      sleep 1
    done
    [[ "${wake_ready}" -eq 1 ]] || return 1
  fi
  if durable_release; then
    if durable_duty_cutoffs; then duty_off_markers off_markers || return 1; fi
    if [[ "${preserve_existing_duty}" -eq 1 ]]; then
      duty_requested_at="$(sed -n 's/^AUTOSTOP_WORK_TELEGRAM_DUTY_REQUESTED_AT=//p' "${monitor_env}")" || return 1
    else
      duty_requested_at="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" || return 1
    fi
    if [[ -n "${duty_requested_at}" ]]; then
      printf 'AUTOSTOP_WORK_TELEGRAM_DUTY_REQUESTED_AT=%s\n' "${duty_requested_at}" >> "${pending_monitor_env}" || return 1
    fi
  fi
  chown root:root "${pending_monitor_env}" || return 1
  chmod 0644 "${pending_monitor_env}" || return 1
  if [[ ! -f "${monitor_env}" ]] || ! cmp -s "${pending_monitor_env}" "${monitor_env}"; then
    intent_changed=1
  fi
  mv -T -- "${pending_monitor_env}" "${monitor_env}" || return 1
  pending_monitor_env=""
  published_duty_intent=1
  if [[ "${#off_markers[@]}" -gt 0 ]] || { [[ "${intent_changed}" -eq 1 ]] && durable_duty_cutoffs; }; then
    # An auto-restart can race an inactive status check. Force a boot after
    # publishing a changed intent or when OFF markers still need a fresh boot.
    systemctl enable "${service_unit}" || return 1
    systemctl restart "${service_unit}" || return 1
  elif systemctl is-active --quiet "${service_unit}"; then
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
      && monitor_status="$(sudo -u "${service_user}" env PYTHONSAFEPATH=1 PYTHONPATH="${release_link}" "${venv_python}" -m autostop_manager.telegram_bridge --account work monitor-status)" \
      && grep -Eq '"enabled": true' <<<"${monitor_status}" \
      && grep -Eq '"retention":[[:space:]]*"(memory_only|durable_refs)"' <<<"${monitor_status}"; then
      if durable_release; then
        bridge_control duty-set --enabled >/dev/null || return 1
        if [[ -f "${wake_config}" ]]; then
          wake_control resume >/dev/null || return 1
        fi
        prune_duty_off_markers "${off_markers[@]}" || return 1
      fi
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_wake_after_incomplete_enable() {
  local failed=0
  [[ "${wake_activation_attempted}" -eq 1 ]] || return 0
  if [[ "${wake_was_active}" -eq 0 ]]; then
    systemctl stop "${wake_unit}" || failed=1
  fi
  if [[ "${wake_was_enabled}" -eq 0 ]]; then
    systemctl disable "${wake_unit}" || failed=1
  fi
  return "${failed}"
}

cleanup_incomplete_enable() {
  local exit_code="$?"
  if [[ -n "${pending_monitor_env}" && -f "${pending_monitor_env}" && ! -L "${pending_monitor_env}" ]]; then unlink -- "${pending_monitor_env}" || true; fi
  # A failed repeated enable must preserve an existing queue or blocked turn.
  if [[ "${preserve_existing_duty}" -eq 0 ]] && durable_duty_cutoffs; then
    restore_wake_after_incomplete_enable || echo "work_telegram_wake_rollback_incomplete=true" >&2
    if [[ "${published_duty_intent}" -eq 1 ]]; then
      disable_duty 0 || echo "work_telegram_duty_rollback_incomplete=true" >&2
    fi
  elif [[ "${preserve_existing_duty}" -eq 0 ]] && ! disable_duty; then
    # A starting bridge may have no socket yet. Withdraw the new intent and
    # reload the service environment even when the normal RPC rollback failed.
    if [[ "${published_duty_intent}" -eq 1 ]]; then
      [[ ! -e "${monitor_env}" ]] || unlink -- "${monitor_env}" || true
      systemctl restart "${service_unit}" || true
    fi
  fi
  return "${exit_code}"
}

case "${operation}" in
  --status)
    load_bridge_python || exit 1
    bridge_state="$(bridge_status)" || { printf '%s\n' '{"ok":false,"transport_ready":false,"error":"bridge_unavailable"}'; exit 1; }
    wake_active=false
    if [[ -f "${wake_config}" ]] && systemctl is-active --quiet "${wake_unit}"; then wake_active=true; fi
    wake_state='{}'
    if [[ "${wake_active}" == true ]]; then
      wake_state="$(wake_control status)" \
        || wake_state='{}'
    fi
    BRIDGE_STATE="${bridge_state}" WAKE_ACTIVE="${wake_active}" WAKE_STATE="${wake_state}" "${venv_python}" -c '
import json, os
s=json.loads(os.environ["BRIDGE_STATE"])
try:
  w=json.loads(os.environ["WAKE_STATE"])
except (ValueError, TypeError):
  w={}
if not isinstance(w, dict):
  w={}
def observed_count(value):
  return value if w.get("ok") is True and type(value) is int and value >= 0 else None
inbound=bool(s.get("inbound_enabled"))
health=s.get("intake_health")
health=health if isinstance(health, dict) else {}
health_state=health.get("state")
health_error=health.get("error_code")
states={"ready", "running", "waiting_thread", "held", "paused", "offline", "connected"}
codes={"inbound_monitor_full", "inbound_capture_failed", "inbound_storage_unavailable", "history_reconciliation_changed", "history_reconciliation_failed", "history_reconciliation_incomplete", "history_reconciliation_unverified", "intake_held", "intake_offline"}
if (s.get("ok") is not True or "state" not in health or "error_code" not in health
    or not isinstance(health_state, str) or health_state not in states
    or (health_error is not None and not isinstance(health_error, str))
    or (health_state == "ready" and health_error is not None)
    or (health_state in {"held", "offline"} and not health_error)):
  health_state="unknown"
  health_error=None
elif health_error is not None and health_error not in codes:
  health_error="other"
reconciliation=s.get("intake_reconciliation")
if s.get("ok") is not True or not isinstance(reconciliation, str) or reconciliation not in {"unknown", "in_progress", "held", "complete"}:
  reconciliation="unknown"
elif reconciliation == "complete" and (health_state != "ready" or health_error is not None or s.get("inbound_enabled") is not True or s.get("transport_ready") is not True):
  reconciliation="unknown"
connected=bool(w.get("ok") and w.get("connected"))
ready=bool(w.get("ok") and w.get("enabled") and w.get("ready", connected))
recovery_state=w.get("recovery_state", "ready" if ready else "unavailable")
if not isinstance(recovery_state, str) or recovery_state not in {"starting", "ready", "reconnecting", "reconciling", "blocked", "paused", "unavailable", "waiting_thread", "waiting_bridge", "held", "running"}:
  recovery_state="unavailable"
execution_state=w.get("execution_state", recovery_state)
if not isinstance(execution_state, str) or execution_state not in {"starting", "ready", "reconnecting", "reconciling", "blocked", "paused", "unavailable", "waiting_thread", "waiting_bridge", "held", "running", "idle"}:
  execution_state="unavailable"
print(json.dumps({
  "ok": bool(s.get("ok")),
  "transport_ready": bool(s.get("transport_ready")),
  "inbound_enabled": inbound,
  "intake_health": {"state": health_state, "error_code": health_error},
  "intake_reconciliation": reconciliation,
  "owner_notification_configured": bool(s.get("owner_notification_configured")),
  "wake_active": os.environ["WAKE_ACTIVE"] == "true",
  "wake_connected": connected,
  "wake_ready": ready,
  "wake_recovery_state": recovery_state,
  "execution_state": execution_state,
  "pending": observed_count(w.get("pending", w.get("queued"))),
  "needs_review": observed_count(w.get("needs_review")),
  "status_fresh": bool(w.get("ok") and w.get("status_fresh") is True),
  "state": "inbound_enabled" if inbound else "outbound_only",
  "polling": False,
}, separators=(",", ":"), sort_keys=True))'
    ;;
  --disable)
    disable_duty || { echo "work_telegram_duty_disable_failed=true" >&2; exit 1; }
    printf '%s\n' "work_telegram_duty=disabled" "monitoring=off" "outbound=ready"
    ;;
  --enable)
    if [[ -f "${monitor_env}" ]] && grep -qx 'AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1' "${monitor_env}"; then
      preserve_existing_duty=1
    fi
    trap cleanup_incomplete_enable EXIT
    enable_duty || { echo "work_telegram_duty_enable_failed=true" >&2; exit 1; }
    trap - EXIT
    if durable_release; then
      printf '%s\n' "work_telegram_duty=enabled" "monitoring=enabled" "retention=durable_refs"
    else
      printf '%s\n' "work_telegram_duty=enabled" "monitoring=enabled" "retention=memory_only"
    fi
    ;;
  *)
    usage; exit 2
    ;;
esac
