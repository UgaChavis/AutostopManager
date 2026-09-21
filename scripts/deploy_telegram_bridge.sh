#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="/opt/AutostopManager"
BRANCH="AutostopManager"

usage() {
  echo "usage: $0 --account personal|work [--no-start] [revision]" >&2
}

if [[ $# -lt 2 || "$1" != "--account" ]]; then
  usage
  exit 2
fi
account="$2"
shift 2
no_start=0
revision=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start)
      no_start=1
      ;;
    --*)
      usage
      exit 2
      ;;
    *)
      if [[ -n "${revision}" ]]; then
        usage
        exit 2
      fi
      revision="$1"
      ;;
  esac
  shift
done

case "${account}" in
  personal)
    release_root="/opt/autostop-telegram-releases"
    unit_path="/etc/systemd/system/autostop-telegram.service"
    unit_relative_path="deploy/systemd/autostop-telegram.service"
    service_unit="autostop-telegram.service"
    service_user="autostop-telegram"
    venv_root="/opt/autostop-telegram-venv"
    ;;
  work)
    release_root="/opt/autostop-work-telegram-releases"
    unit_path="/etc/systemd/system/autostop-work-telegram.service"
    unit_relative_path="deploy/systemd/autostop-work-telegram.service"
    service_unit="autostop-work-telegram.service"
    service_user="autostop-work-telegram"
    venv_root="/opt/autostop-work-telegram-venv"
    work_runtime_root="/opt/autostop-work-telegram-runtimes"
    work_model_link="/opt/autostop-work-telegram-models/faster-whisper-small"
    media_wrapper_path="/usr/local/sbin/autostop-work-telegram-media"
    monitor_env="/etc/autostop-work-telegram/monitor.env"
    ;;
  *)
    echo "account_invalid=true" >&2
    exit 2
    ;;
esac

if [[ "${no_start}" -eq 1 && "${account}" != "work" ]]; then
  echo "ERROR: --no-start is available only for the work account" >&2
  exit 2
fi
if [[ "${account}" == "work" && "${no_start}" -ne 1 ]]; then
  echo "ERROR: work runtime releases require --no-start as an explicit lifecycle guard" >&2
  exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi
if [[ "${account}" == "work" ]]; then
  control_lock="/run/autostop-work-telegram-control.lock"
  exec 9>"${control_lock}"
  flock -x 9
fi

if ! git -C "${SOURCE_DIR}" fetch --quiet --prune origin "refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"; then
  echo "ERROR: Telegram release Git fetch failed" >&2
  exit 1
fi
remote_revision="$(git -C "${SOURCE_DIR}" rev-parse "origin/${BRANCH}^{commit}")"
head_revision="$(git -C "${SOURCE_DIR}" rev-parse "HEAD^{commit}")"
live_remote_revision="$(
  git -C "${SOURCE_DIR}" ls-remote --heads origin "refs/heads/${BRANCH}" | awk 'NR == 1 { print $1 }'
)"
if [[ -z "${live_remote_revision}" || "${head_revision}" != "${remote_revision}" \
  || "${remote_revision}" != "${live_remote_revision}" ]]; then
  echo "ERROR: Telegram release checkout must match the fetched remote branch" >&2
  exit 1
fi
if ! git -C "${SOURCE_DIR}" diff --quiet --ignore-submodules \
  || ! git -C "${SOURCE_DIR}" diff --cached --quiet --ignore-submodules \
  || [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain --untracked-files=all)" ]]; then
  echo "ERROR: Telegram release checkout must be clean" >&2
  exit 1
fi
if [[ -z "${revision}" ]]; then
  revision="${remote_revision}"
fi
revision="$(git -C "${SOURCE_DIR}" rev-parse "${revision}^{commit}")"
if [[ "${revision}" != "${remote_revision}" ]]; then
  echo "ERROR: Telegram release revision must match origin/${BRANCH}" >&2
  exit 1
fi
if [[ "${account}" == "work" ]]; then
  automation_group_helper="${SOURCE_DIR}/scripts/ensure-automation-group.sh"
  if [[ ! -f "${automation_group_helper}" || -L "${automation_group_helper}" \
    || ! -x "${automation_group_helper}" ]]; then
    echo "ERROR: automation group helper is unavailable" >&2
    exit 1
  fi
  if ! "${automation_group_helper}" >/dev/null; then
    echo "ERROR: automation group is unavailable" >&2
    exit 1
  fi
fi

work_runtime_dir=""
candidate_work_venv=""
candidate_work_model=""
previous_work_venv=""
previous_work_model=""
work_runtime_switched=0
current_link="${release_root}/current"
previous_release="$(readlink -f "${current_link}" 2>/dev/null || true)"
work_service_was_active=0
work_inbound_expected=0

if [[ "${account}" == "work" ]]; then
  work_load_state="$(systemctl show --property=LoadState --value "${service_unit}" 2>/dev/null || true)"
  if [[ "${work_load_state}" == "not-found" ]]; then
    [[ -z "${previous_release}" && ! -e "${current_link}" && ! -L "${current_link}" ]] || {
      echo "ERROR: missing work Telegram service requires a clean first release" >&2
      exit 1
    }
  else
    [[ "${work_load_state}" == "loaded" ]] || {
      echo "ERROR: work Telegram service lifecycle must be recovered before release" >&2
      exit 1
    }
    work_active_state="$(systemctl show --property=ActiveState --value "${service_unit}")"
    work_unit_file_state="$(systemctl show --property=UnitFileState --value "${service_unit}")"
    if [[ "${work_active_state}" == "active" && "${work_unit_file_state}" == "enabled" ]]; then
      work_service_was_active=1
    elif [[ "${work_active_state}" != "inactive" \
      || ( "${work_unit_file_state}" != "disabled" && "${work_unit_file_state}" != "disabled-runtime" ) ]]; then
      echo "ERROR: work Telegram service lifecycle must be active-enabled or legacy inactive-disabled" >&2
      exit 1
    fi
  fi
  if [[ -e "${monitor_env}" || -L "${monitor_env}" ]]; then
    [[ -f "${monitor_env}" && ! -L "${monitor_env}" \
      && "$(stat -c '%U:%G:%a' "${monitor_env}")" == "root:root:644" ]] || {
      echo "ERROR: work Telegram inbound intent is invalid" >&2
      exit 1
    }
    work_inbound_expected=1
  fi
fi

release_id="$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:12}"
release_dir="${release_root}/${release_id}"
staging_dir="${release_dir}.partial-$$"
initial_unit_backup=""
initial_media_wrapper_backup=""

cleanup() {
  [[ "${staging_dir}" == "${release_root}"/*.partial-* ]] || return 1
  [[ -d "${staging_dir}" && ! -L "${staging_dir}" ]] || return 0
  find -P "${staging_dir}" -mindepth 1 -delete
  rmdir -- "${staging_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "${release_root}"
if [[ -e "${current_link}" || -L "${current_link}" ]] \
  && { [[ ! -L "${current_link}" ]] || [[ -z "${previous_release}" ]] || [[ ! -d "${previous_release}" ]]; }; then
  echo "ERROR: existing Telegram current release link is invalid" >&2
  exit 1
fi
if [[ -e "${release_dir}" || -L "${release_dir}" ]]; then
  echo "ERROR: release already exists" >&2
  exit 1
fi

prepare_work_runtime_candidate() {
  local candidate_manifest previous_target
  [[ "${account}" == "work" ]] || return 0
  work_runtime_dir="${work_runtime_root}/${revision}"
  candidate_work_venv="${work_runtime_dir}/venv"
  candidate_work_model="${work_runtime_dir}/model"
  candidate_manifest="${work_runtime_dir}/faster-whisper-small.sha256"
  [[ -d "${work_runtime_dir}" && ! -L "${work_runtime_dir}" \
    && -d "${candidate_work_venv}" && ! -L "${candidate_work_venv}" \
    && -x "${candidate_work_venv}/bin/python" \
    && -d "${candidate_work_model}" && ! -L "${candidate_work_model}" \
    && -f "${work_runtime_dir}/.dependencies-ready" && ! -L "${work_runtime_dir}/.dependencies-ready" \
    && "$(stat -c '%U:%G:%a' "${work_runtime_dir}/.dependencies-ready")" == "root:root:600" \
    && -f "${work_runtime_dir}/.model-ready" && ! -L "${work_runtime_dir}/.model-ready" \
    && "$(stat -c '%U:%G:%a' "${work_runtime_dir}/.model-ready")" == "root:root:600" \
    && -f "${candidate_manifest}" && ! -L "${candidate_manifest}" \
    && "$(stat -c '%U:%G:%a' "${candidate_manifest}")" == "root:root:600" ]] || return 1
  git -C "${SOURCE_DIR}" show "${revision}:deploy/telegram/faster-whisper-small.sha256" \
    | cmp -s - "${candidate_manifest}" || return 1
  if [[ -e "${venv_root}" || -L "${venv_root}" ]]; then
    [[ -L "${venv_root}" ]] || return 1
    previous_target="$(readlink -f -- "${venv_root}" 2>/dev/null || true)"
    [[ "${previous_target}" == "${work_runtime_root}/"* && -d "${previous_target}" && ! -L "${previous_target}" ]] || return 1
    previous_work_venv="${previous_target}"
  elif [[ -n "${previous_release}" ]]; then
    return 1
  fi
  if [[ -e "${work_model_link}" || -L "${work_model_link}" ]]; then
    [[ -L "${work_model_link}" ]] || return 1
    previous_target="$(readlink -f -- "${work_model_link}" 2>/dev/null || true)"
    [[ "${previous_target}" == "${work_runtime_root}/"* && -d "${previous_target}" && ! -L "${previous_target}" ]] || return 1
    previous_work_model="${previous_target}"
  fi
}

switch_work_runtime_link() {
  local link_path="$1" target_path="$2" next_link
  [[ "${target_path}" == "${work_runtime_root}/"* && -d "${target_path}" && ! -L "${target_path}" ]] || return 1
  [[ -L "${link_path}" || ! -e "${link_path}" ]] || return 1
  next_link="${link_path}.next-${release_id}"
  [[ ! -e "${next_link}" && ! -L "${next_link}" ]] || return 1
  ln -s "${target_path}" "${next_link}" || return 1
  if ! mv -Tf -- "${next_link}" "${link_path}"; then
    unlink -- "${next_link}" 2>/dev/null || true
    return 1
  fi
}

restore_work_runtime_link() {
  local link_path="$1" target_path="$2"
  [[ -L "${link_path}" || ! -e "${link_path}" ]] || return 1
  if [[ -n "${target_path}" ]]; then
    switch_work_runtime_link "${link_path}" "${target_path}"
  elif [[ -L "${link_path}" ]]; then
    unlink -- "${link_path}"
  fi
}

activate_work_runtime() {
  [[ "${account}" == "work" ]] || return 0
  switch_work_runtime_link "${venv_root}" "${candidate_work_venv}" || return 1
  work_runtime_switched=1
  if ! switch_work_runtime_link "${work_model_link}" "${candidate_work_model}"; then
    restore_work_runtime || return 1
    return 1
  fi
}

restore_work_runtime() {
  [[ "${account}" == "work" && "${work_runtime_switched}" -eq 1 ]] || return 0
  restore_work_runtime_link "${work_model_link}" "${previous_work_model}" || return 1
  restore_work_runtime_link "${venv_root}" "${previous_work_venv}" || return 1
  work_runtime_switched=0
}

if ! prepare_work_runtime_candidate; then
  echo "ERROR: work Telegram runtime candidate is missing or invalid for this revision" >&2
  exit 1
fi

backup_initial_release_assets() {
  if [[ -n "${previous_release}" ]]; then
    return 0
  fi
  if [[ -e "${unit_path}" || -L "${unit_path}" ]]; then
    [[ -f "${unit_path}" && ! -L "${unit_path}" ]] || return 1
    initial_unit_backup="${release_root}/.unit-before-${release_id}"
    cp --no-dereference -- "${unit_path}" "${initial_unit_backup}" || return 1
  fi
  if [[ "${account}" == "work" && ( -e "${media_wrapper_path}" || -L "${media_wrapper_path}" ) ]]; then
    [[ -f "${media_wrapper_path}" && ! -L "${media_wrapper_path}" ]] || return 1
    initial_media_wrapper_backup="${release_root}/.media-before-${release_id}"
    cp --no-dereference -- "${media_wrapper_path}" "${initial_media_wrapper_backup}" || return 1
  fi
}

cleanup_initial_release_backups() {
  if [[ -n "${initial_unit_backup}" && -f "${initial_unit_backup}" && ! -L "${initial_unit_backup}" ]]; then
    unlink -- "${initial_unit_backup}"
  fi
  if [[ -n "${initial_media_wrapper_backup}" && -f "${initial_media_wrapper_backup}" \
    && ! -L "${initial_media_wrapper_backup}" ]]; then
    unlink -- "${initial_media_wrapper_backup}"
  fi
}

if ! backup_initial_release_assets; then
  echo "ERROR: existing Telegram release assets cannot be safely backed up" >&2
  exit 1
fi
install -d -o root -g root -m 0755 "${staging_dir}"
git -C "${SOURCE_DIR}" archive --format=tar "${revision}" | tar -xf - -C "${staging_dir}"
chown -R root:root "${staging_dir}"
find "${staging_dir}" -type d -exec chmod 0755 {} +
find "${staging_dir}" -type f -exec chmod 0644 {} +
if [[ "${account}" == "work" ]]; then
  duty_controller_source="${staging_dir}/scripts/set-work-telegram-duty.sh"
  media_wrapper_source="${staging_dir}/scripts/run-work-telegram-media.sh"
  monitor_voice_wrapper_source="${staging_dir}/scripts/run-work-telegram-monitor-voice.sh"
  if [[ ! -f "${duty_controller_source}" || -L "${duty_controller_source}" ]]; then
    echo "ERROR: work Telegram duty controller missing from release" >&2
    exit 1
  fi
  if [[ ! -f "${media_wrapper_source}" || -L "${media_wrapper_source}" ]]; then
    echo "ERROR: work media sandbox wrapper missing from release" >&2
    exit 1
  fi
  if [[ ! -f "${monitor_voice_wrapper_source}" || -L "${monitor_voice_wrapper_source}" ]]; then
    echo "ERROR: work monitored-voice wrapper missing from release" >&2
    exit 1
  fi
  chmod 0755 "${duty_controller_source}" "${media_wrapper_source}" "${monitor_voice_wrapper_source}"
fi
mv -- "${staging_dir}" "${release_dir}"

bridge_ready() {
  local status
  systemctl is-active --quiet "${service_unit}" || return 1
  if [[ "${account}" != "work" ]]; then
    sudo -u "${service_user}" env PYTHONPATH="${current_link}" \
      "${venv_root}/bin/python" -m autostop_manager.telegram_bridge --account "${account}" probe \
      | grep -Eq '"authorized":[[:space:]]*true'
    return
  fi
  status="$(
    sudo -u "${service_user}" env PYTHONPATH="${current_link}" \
      "${venv_root}/bin/python" -m autostop_manager.telegram_bridge --account work status
  )" || return 1
  grep -Eq '"authorized":[[:space:]]*true' <<<"${status}" \
    && grep -Eq '"transport_ready":[[:space:]]*true' <<<"${status}" \
    && grep -Eq "\"inbound_enabled\":[[:space:]]*$([[ "${work_inbound_expected}" -eq 1 ]] && echo true || echo false)" \
      <<<"${status}"
}

start_work_bridge() {
  systemctl enable --now "${service_unit}" || return 1
  for _attempt in $(seq 1 15); do
    if bridge_ready; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_media_wrapper() {
  local previous_media_wrapper
  if [[ "${account}" != "work" ]]; then
    return 0
  fi
  previous_media_wrapper="${previous_release}/scripts/run-work-telegram-media.sh"
  if [[ -f "${previous_media_wrapper}" && ! -L "${previous_media_wrapper}" ]]; then
    install -o root -g root -m 0755 "${previous_media_wrapper}" "${media_wrapper_path}"
  elif [[ -e "${media_wrapper_path}" || -L "${media_wrapper_path}" ]]; then
    unlink -- "${media_wrapper_path}"
  fi
}

restore_previous_release_assets() {
  local rollback_link current_target
  restore_work_runtime || return 1
  if [[ -z "${previous_release}" ]]; then
    current_target="$(readlink -f "${current_link}" 2>/dev/null || true)"
    if [[ "${current_target}" == "${release_dir}" ]]; then
      unlink -- "${current_link}" || return 1
    fi
    if [[ -n "${initial_unit_backup}" ]]; then
      install -o root -g root -m 0644 "${initial_unit_backup}" "${unit_path}" || return 1
    elif [[ -e "${unit_path}" || -L "${unit_path}" ]]; then
      unlink -- "${unit_path}" || return 1
    fi
    if [[ "${account}" == "work" ]]; then
      if [[ -n "${initial_media_wrapper_backup}" ]]; then
        install -o root -g root -m 0755 "${initial_media_wrapper_backup}" "${media_wrapper_path}" || return 1
      elif [[ -e "${media_wrapper_path}" || -L "${media_wrapper_path}" ]]; then
        unlink -- "${media_wrapper_path}" || return 1
      fi
    fi
    systemctl daemon-reload || return 1
    cleanup_initial_release_backups
    return 0
  fi
  if [[ ! -d "${previous_release}" || ! -f "${previous_release}/${unit_relative_path}" ]]; then
    return 1
  fi
  rollback_link="${release_root}/.rollback-${release_id}"
  ln -s "${previous_release}" "${rollback_link}" || return 1
  mv -Tf -- "${rollback_link}" "${current_link}" || return 1
  if ! install -o root -g root -m 0644 "${previous_release}/${unit_relative_path}" "${unit_path}"; then
    return 1
  fi
  restore_media_wrapper || return 1
  systemctl daemon-reload || return 1
}

activate_new_release_assets() {
  local next_link
  if ! install -o root -g root -m 0644 "${release_dir}/${unit_relative_path}" "${unit_path}"; then
    return 1
  fi
  if ! systemctl daemon-reload; then
    return 1
  fi
  activate_work_runtime || return 1
  next_link="${release_root}/.current-${release_id}"
  if ! ln -s "${release_dir}" "${next_link}"; then
    return 1
  fi
  if ! mv -Tf -- "${next_link}" "${current_link}"; then
    unlink -- "${next_link}" 2>/dev/null || true
    return 1
  fi
}

rollback() {
  if [[ "${account}" == "work" ]]; then
    systemctl stop "${service_unit}" || return 1
  fi
  restore_previous_release_assets || return 1
  if ! systemctl enable --now "${service_unit}"; then
    return 1
  fi
  for _rollback_attempt in $(seq 1 15); do
    if bridge_ready; then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_current_media_wrapper() {
  if [[ "${account}" != "work" ]]; then
    return 0
  fi
  media_wrapper_source="${release_dir}/scripts/run-work-telegram-media.sh"
  [[ -f "${media_wrapper_source}" && ! -L "${media_wrapper_source}" ]] || return 1
  install -o root -g root -m 0755 "${media_wrapper_source}" "${media_wrapper_path}"
}

transcription_runtime_ready() {
  if [[ "${account}" == "work" ]]; then
    "${media_wrapper_path}" self-check || return 1
    "${current_link}/scripts/run-work-telegram-monitor-voice.sh" --self-check >/dev/null || return 1
    return
  fi
  sudo -u "${service_user}" env PYTHONPATH="${current_link}" HF_HUB_OFFLINE=1 \
    /usr/bin/timeout --signal=TERM --kill-after=10s 120s \
    "${venv_root}/bin/python" -m autostop_manager.telegram_transcribe --account personal --self-check
}

if [[ "${account}" == "work" && "${work_service_was_active}" -eq 1 ]]; then
  if ! systemctl stop "${service_unit}"; then
    echo "ERROR: work Telegram transport did not enter the bounded release stop" >&2
    exit 1
  fi
fi

if ! activate_new_release_assets; then
  restore_succeeded=0
  if restore_previous_release_assets; then
    restore_succeeded=1
  fi
  if [[ "${restore_succeeded}" -eq 1 && "${account}" == "work" && "${work_service_was_active}" -eq 1 ]]; then
    start_work_bridge || restore_succeeded=0
  fi
  if [[ "${restore_succeeded}" -eq 1 ]]; then
    echo "ERROR: Telegram release activation failed; previous release assets restored" >&2
  else
    echo "ERROR: Telegram release activation failed; previous runtime was not fully restored" >&2
  fi
  exit 1
fi

if ! install_current_media_wrapper; then
  restore_succeeded=0
  if restore_previous_release_assets; then
    restore_succeeded=1
  fi
  if [[ "${restore_succeeded}" -eq 1 && "${account}" == "work" && "${work_service_was_active}" -eq 1 ]]; then
    start_work_bridge || restore_succeeded=0
  fi
  if [[ "${restore_succeeded}" -eq 1 ]]; then
    echo "ERROR: work media sandbox wrapper install failed; previous release assets restored" >&2
  else
    echo "ERROR: work media sandbox wrapper install failed; previous runtime was not fully restored" >&2
  fi
  exit 1
fi

if [[ "${account}" == "work" ]]; then
  if ! start_work_bridge; then
    if rollback; then
      echo "ERROR: work Telegram bridge readiness failed; previous release restored" >&2
    else
      echo "ERROR: work Telegram bridge readiness failed; no previous Telegram release exists" >&2
    fi
    exit 1
  fi
  if ! transcription_runtime_ready; then
    if rollback; then
      echo "ERROR: local Telegram transcription runtime failed; previous release assets restored" >&2
    else
      echo "ERROR: local Telegram transcription runtime failed; previous runtime was not fully restored" >&2
    fi
    exit 1
  fi
  echo "telegram_bridge_deployed=true"
  echo "account=work"
  echo "outbound=ready"
  echo "inbound_restored=$([[ "${work_inbound_expected}" -eq 1 ]] && echo true || echo false)"
  cleanup_initial_release_backups
  exit 0
fi

if ! systemctl restart "${service_unit}"; then
  if rollback; then
    echo "ERROR: Telegram service restart failed; previous release restored" >&2
  else
    echo "ERROR: Telegram service restart failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi
ready=0
for _attempt in $(seq 1 15); do
  if bridge_ready; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" -ne 1 ]]; then
  if rollback; then
    echo "ERROR: Telegram bridge readiness failed; previous release restored" >&2
  else
    echo "ERROR: Telegram bridge readiness failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi
if ! transcription_runtime_ready; then
  if rollback; then
    echo "ERROR: local Telegram transcription runtime failed; previous release restored" >&2
  else
    echo "ERROR: local Telegram transcription runtime failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi

cleanup_initial_release_backups
echo "telegram_bridge_deployed=true"
echo "account=${account}"
echo "revision=${revision}"
