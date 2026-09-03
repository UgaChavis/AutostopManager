#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="/opt/AutostopManager"
BRANCH="AutostopManager"

if [[ $# -ne 4 || "$1" != "--account" || "$2" != "work" || "$3" != "--revision" ]]; then
  echo "usage: $0 --account work --revision <commit>" >&2
  exit 2
fi
requested_revision="$4"
release_revision=""

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi

source_user="autostop-telegram"
target_user="autostop-work-telegram"
target_venv="/opt/autostop-work-telegram-venv/bin/python"
source_model="/var/lib/autostop-telegram/models/faster-whisper-small"
manifest_path="/etc/autostop-telegram-transcription-models/faster-whisper-small.sha256"
model_root="/opt/autostop-work-telegram-models"
target_model="${model_root}/faster-whisper-small"
model_files=(
  "config.json"
  "model.bin"
  "tokenizer.json"
  "vocabulary.txt"
)
staging_dir=""
target_created=0

validate_release_sources() {
  local remote_revision head_revision worktree_status
  release_revision="$(git -C "${SOURCE_DIR}" rev-parse "${requested_revision}^{commit}")" || return 1
  remote_revision="$(git -C "${SOURCE_DIR}" rev-parse "origin/${BRANCH}")" || return 1
  head_revision="$(git -C "${SOURCE_DIR}" rev-parse HEAD)" || return 1
  [[ "${release_revision}" == "${remote_revision}" && "${release_revision}" == "${head_revision}" ]] || return 1
  git -C "${SOURCE_DIR}" diff --quiet --ignore-submodules || return 1
  git -C "${SOURCE_DIR}" diff --cached --quiet --ignore-submodules || return 1
  worktree_status="$(git -C "${SOURCE_DIR}" status --porcelain --untracked-files=all)"
  [[ -z "${worktree_status}" ]]
}

cleanup_directory() {
  local directory="$1"
  [[ "${directory}" == "${model_root}"/.faster-whisper-small.* || "${directory}" == "${target_model}" ]] || return 1
  [[ -d "${directory}" && ! -L "${directory}" ]] || return 0
  find -P "${directory}" -mindepth 1 -delete
  rmdir -- "${directory}"
}

cleanup() {
  cleanup_directory "${staging_dir}" || true
  if [[ "${target_created}" -eq 1 ]]; then
    cleanup_directory "${target_model}" || true
  fi
}
trap cleanup EXIT

validate_manifest() {
  [[ -f "${manifest_path}" && ! -L "${manifest_path}" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "${manifest_path}")" == "root:root:600" ]] || return 1
}

verify_manifest() {
  local model_dir="$1"
  (cd "${model_dir}" && sha256sum -c --status "${manifest_path}")
}

validate_source_layout() {
  local relative_path candidate_path resolved_path
  [[ -d "${source_model}" && ! -L "${source_model}" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "${source_model}")" == "${source_user}:${source_user}:700" ]] || return 1
  for relative_path in "${model_files[@]}"; do
    candidate_path="${source_model}/${relative_path}"
    [[ -f "${candidate_path}" && ! -L "${candidate_path}" ]] || return 1
    resolved_path="$(readlink -f -- "${candidate_path}")"
    [[ "${resolved_path}" == "${source_model}/"* ]] || return 1
    [[ "$(stat -c '%U:%G:%a' "${candidate_path}")" == "${source_user}:${source_user}:600" ]] || return 1
  done
}

validate_target_layout() {
  local relative_path candidate_path
  [[ -d "${target_model}" && ! -L "${target_model}" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "${target_model}")" == "root:${target_user}:750" ]] || return 1
  for relative_path in "${model_files[@]}"; do
    candidate_path="${target_model}/${relative_path}"
    [[ -f "${candidate_path}" && ! -L "${candidate_path}" ]] || return 1
    [[ "$(stat -c '%U:%G:%a' "${candidate_path}")" == "root:${target_user}:640" ]] || return 1
  done
}

validate_target_runtime() {
  sudo -u "${target_user}" env HF_HUB_OFFLINE=1 \
    "${target_venv}" -c \
    "from faster_whisper import WhisperModel; WhisperModel('${target_model}', device='cpu', compute_type='int8', cpu_threads=1, num_workers=1, local_files_only=True)"
}

if [[ ! -x "${target_venv}" ]]; then
  echo "transcription_venv_unavailable=true" >&2
  exit 1
fi
if ! validate_release_sources; then
  echo "telegram_release_source_invalid=true" >&2
  exit 1
fi
if ! validate_manifest; then
  echo "transcription_model_manifest_invalid=true" >&2
  exit 1
fi
if ! git -C "${SOURCE_DIR}" show "${release_revision}:deploy/telegram/faster-whisper-small.sha256" \
  | cmp -s - "${manifest_path}"; then
  echo "transcription_model_manifest_revision_mismatch=true" >&2
  exit 1
fi
if [[ -L "${model_root}" || ( -e "${model_root}" && ! -d "${model_root}" ) ]]; then
  echo "work_transcription_model_root_invalid=true" >&2
  exit 1
fi
if [[ ! -d "${model_root}" ]]; then
  install -d -m 0750 -o root -g "${target_user}" "${model_root}"
fi
if [[ "$(stat -c '%U:%G:%a' "${model_root}")" != "root:${target_user}:750" ]]; then
  echo "work_transcription_model_root_invalid=true" >&2
  exit 1
fi
if [[ -L "${target_model}" || ( -e "${target_model}" && ! -d "${target_model}" ) ]]; then
  echo "work_transcription_model_invalid=true" >&2
  exit 1
fi

if [[ -d "${target_model}" ]]; then
  if ! validate_target_layout || ! verify_manifest "${target_model}" || ! validate_target_runtime; then
    echo "work_transcription_model_invalid=true" >&2
    exit 1
  fi
  trap - EXIT
  echo "work_transcription_model_ready=true"
  exit 0
fi

if ! validate_source_layout || ! verify_manifest "${source_model}"; then
  echo "source_transcription_model_invalid=true" >&2
  exit 1
fi

staging_dir="$(mktemp -d "${model_root}/.faster-whisper-small.XXXXXX")"
for relative_path in "${model_files[@]}"; do
  install -d -m 0700 -o root -g root "${staging_dir}/$(dirname "${relative_path}")"
  cp --no-dereference -- "${source_model}/${relative_path}" "${staging_dir}/${relative_path}"
  chmod 0600 "${staging_dir}/${relative_path}"
done
if find -P "${staging_dir}" -type l -print -quit | grep -q .; then
  echo "work_transcription_model_invalid=true" >&2
  exit 1
fi
if ! verify_manifest "${staging_dir}"; then
  echo "source_transcription_model_changed=true" >&2
  exit 1
fi
chown -R root:"${target_user}" "${staging_dir}"
find -P "${staging_dir}" -type d -exec chmod 0750 {} +
find -P "${staging_dir}" -type f -exec chmod 0640 {} +
mv -T -- "${staging_dir}" "${target_model}"
staging_dir=""
target_created=1
if ! validate_target_layout || ! verify_manifest "${target_model}" || ! validate_target_runtime; then
  echo "work_transcription_model_invalid=true" >&2
  exit 1
fi

target_created=0
trap - EXIT
echo "work_transcription_model_ready=true"
