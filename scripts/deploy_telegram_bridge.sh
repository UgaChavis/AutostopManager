#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="/opt/AutostopManager"
BRANCH="AutostopManager"

if [[ $# -lt 2 || "$1" != "--account" ]]; then
  echo "usage: $0 --account personal|work [revision]" >&2
  exit 2
fi
account="$2"
shift 2
if [[ $# -gt 1 ]]; then
  echo "usage: $0 --account personal|work [revision]" >&2
  exit 2
fi

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
    session_file="/var/lib/autostop-work-telegram/account.session"
    ;;
  *)
    echo "account_invalid=true" >&2
    exit 2
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

revision="${1:-}"
if [[ -z "${revision}" ]]; then
  revision="$(git -C "${SOURCE_DIR}" rev-parse "origin/${BRANCH}")"
fi
revision="$(git -C "${SOURCE_DIR}" rev-parse "${revision}^{commit}")"
remote_revision="$(git -C "${SOURCE_DIR}" rev-parse "origin/${BRANCH}")"
if [[ "${revision}" != "${remote_revision}" ]]; then
  echo "ERROR: Telegram release revision must match origin/${BRANCH}" >&2
  exit 1
fi

was_active=0
if systemctl is-active --quiet "${service_unit}"; then
  was_active=1
fi
if [[ "${account}" == "work" && "${was_active}" -eq 0 ]]; then
  if systemctl is-enabled --quiet "${service_unit}" \
    || [[ -e "${session_file}" || -L "${session_file}" ]]; then
    echo "ERROR: inactive existing work Telegram profile must be recovered before release" >&2
    exit 1
  fi
fi

release_id="$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:12}"
release_dir="${release_root}/${release_id}"
staging_dir="${release_dir}.partial-$$"
current_link="${release_root}/current"
previous_release="$(readlink -f "${current_link}" 2>/dev/null || true)"

cleanup() {
  rm -rf -- "${staging_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "${release_root}"
if [[ -e "${release_dir}" || -L "${release_dir}" ]]; then
  echo "ERROR: release already exists" >&2
  exit 1
fi
install -d -o root -g root -m 0755 "${staging_dir}"
git -C "${SOURCE_DIR}" archive --format=tar "${revision}" | tar -xf - -C "${staging_dir}"
chown -R root:root "${staging_dir}"
find "${staging_dir}" -type d -exec chmod 0755 {} +
find "${staging_dir}" -type f -exec chmod 0644 {} +
mv -- "${staging_dir}" "${release_dir}"

next_link="${release_root}/.current-${release_id}"
ln -s "${release_dir}" "${next_link}"
mv -Tf -- "${next_link}" "${current_link}"
install -o root -g root -m 0644 "${release_dir}/${unit_relative_path}" "${unit_path}"
systemctl daemon-reload

bridge_ready() {
  systemctl is-active --quiet "${service_unit}" \
    && sudo -u "${service_user}" env PYTHONPATH="${current_link}" \
      "${venv_root}/bin/python" -m autostop_manager.telegram_bridge --account "${account}" probe \
      | grep -Eq '"authorized": true'
}

rollback() {
  if [[ -z "${previous_release}" || ! -d "${previous_release}" \
    || ! -f "${previous_release}/${unit_relative_path}" ]]; then
    return 1
  fi
  rollback_link="${release_root}/.rollback-${release_id}"
  ln -s "${previous_release}" "${rollback_link}"
  mv -Tf -- "${rollback_link}" "${current_link}"
  if ! install -o root -g root -m 0644 "${previous_release}/${unit_relative_path}" "${unit_path}"; then
    return 1
  fi
  systemctl daemon-reload
  if ! systemctl restart "${service_unit}"; then
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

if [[ "${account}" == "work" && "${was_active}" -eq 0 ]]; then
  echo "telegram_bridge_deployed=true"
  echo "account=work"
  echo "authorization_required=true"
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
if [[ "${account}" == "personal" ]] \
  && ! sudo -u "${service_user}" env PYTHONPATH="${current_link}" HF_HUB_OFFLINE=1 \
    "${venv_root}/bin/python" -c \
    'from autostop_manager.telegram_transcribe import DEFAULT_MODEL_DIR, _validate_local_model; from faster_whisper import WhisperModel; _validate_local_model(DEFAULT_MODEL_DIR); assert WhisperModel'; then
  if rollback; then
    echo "ERROR: local Telegram transcription runtime failed; previous release restored" >&2
  else
    echo "ERROR: local Telegram transcription runtime failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi

echo "telegram_bridge_deployed=true"
echo "account=${account}"
echo "revision=${revision}"
