#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="/opt/AutostopManager"
RELEASE_ROOT="/opt/autostop-telegram-releases"
UNIT_PATH="/etc/systemd/system/autostop-telegram.service"
BRANCH="AutostopManager"

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

release_id="$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:12}"
release_dir="${RELEASE_ROOT}/${release_id}"
staging_dir="${release_dir}.partial-$$"
current_link="${RELEASE_ROOT}/current"
previous_release="$(readlink -f "${current_link}" 2>/dev/null || true)"

cleanup() {
  rm -rf -- "${staging_dir}"
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "${RELEASE_ROOT}"
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

next_link="${RELEASE_ROOT}/.current-${release_id}"
ln -s "${release_dir}" "${next_link}"
mv -Tf -- "${next_link}" "${current_link}"
install -o root -g root -m 0644 "${release_dir}/deploy/systemd/autostop-telegram.service" "${UNIT_PATH}"
systemctl daemon-reload

rollback() {
  if [[ -z "${previous_release}" || ! -d "${previous_release}" \
    || ! -f "${previous_release}/deploy/systemd/autostop-telegram.service" ]]; then
    return 1
  fi
  rollback_link="${RELEASE_ROOT}/.rollback-${release_id}"
  ln -s "${previous_release}" "${rollback_link}"
  mv -Tf -- "${rollback_link}" "${current_link}"
  if ! install -o root -g root -m 0644 \
    "${previous_release}/deploy/systemd/autostop-telegram.service" "${UNIT_PATH}"; then
    return 1
  fi
  systemctl daemon-reload
  if ! systemctl restart autostop-telegram.service; then
    return 1
  fi
  for _rollback_attempt in $(seq 1 15); do
    if systemctl is-active --quiet autostop-telegram.service \
      && sudo -u autostop-telegram env PYTHONPATH="${current_link}" \
        /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge status >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if ! systemctl restart autostop-telegram.service; then
  if rollback; then
    echo "ERROR: Telegram service restart failed; previous release restored" >&2
  else
    echo "ERROR: Telegram service restart failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi
bridge_ready=0
for _attempt in $(seq 1 15); do
  if systemctl is-active --quiet autostop-telegram.service \
    && sudo -u autostop-telegram env PYTHONPATH="${current_link}" \
      /opt/autostop-telegram-venv/bin/python -m autostop_manager.telegram_bridge status >/dev/null; then
    bridge_ready=1
    break
  fi
  sleep 1
done
if [[ "${bridge_ready}" -ne 1 ]]; then
  if rollback; then
    echo "ERROR: Telegram bridge readiness failed; previous release restored" >&2
  else
    echo "ERROR: Telegram bridge readiness failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi
if ! sudo -u autostop-telegram env PYTHONPATH="${current_link}" HF_HUB_OFFLINE=1 \
  /opt/autostop-telegram-venv/bin/python -c \
  'from autostop_manager.telegram_transcribe import DEFAULT_MODEL_DIR, _validate_local_model; from faster_whisper import WhisperModel; _validate_local_model(DEFAULT_MODEL_DIR); assert WhisperModel' ; then
  if rollback; then
    echo "ERROR: local Telegram transcription runtime failed; previous release restored" >&2
  else
    echo "ERROR: local Telegram transcription runtime failed; no previous Telegram release exists" >&2
  fi
  exit 1
fi

echo "Telegram bridge deployed: ${revision}"
