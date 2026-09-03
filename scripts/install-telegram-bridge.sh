#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="AutostopManager"
requirements_lock="${PROJECT_ROOT}/deploy/telegram/requirements-py312-linux-x86_64.lock"
build_requirements_lock="${PROJECT_ROOT}/deploy/telegram/build-requirements-py312-linux-x86_64.lock"
pyaes_source_lock="${PROJECT_ROOT}/deploy/telegram/pyaes-source-py312-linux-x86_64.lock"
pyaes_wheel_lock="${PROJECT_ROOT}/deploy/telegram/pyaes-wheel-py312-linux-x86_64.lock"
wheelhouse_root="/opt/autostop-telegram-wheelhouse"
pyaes_wheel_name="pyaes-1.6.1-py3-none-any.whl"
pyaes_wheel_path="${wheelhouse_root}/${pyaes_wheel_name}"
pyaes_wheel_sha256="3770d63e03f319be0ea540d35a65ed928eef8c2574b689810ed927a441c088a1"
model_manifest_source="${PROJECT_ROOT}/deploy/telegram/faster-whisper-small.sha256"
model_manifest_dir="/etc/autostop-telegram-transcription-models"
model_manifest_path="${model_manifest_dir}/faster-whisper-small.sha256"
temporary_manifest=""
wheel_staging_dir=""
wheelhouse_staging=""

if [[ $# -ne 4 || "$1" != "--account" || "$3" != "--revision" ]]; then
  echo "usage: $0 --account personal|work --revision <commit>" >&2
  exit 2
fi
account="$2"
requested_revision="$4"
release_revision=""

case "${account}" in
  personal)
    service_user="autostop-telegram"
    state_dir="/var/lib/autostop-telegram"
    config_dir="/etc/autostop-telegram"
    unit_source="${PROJECT_ROOT}/deploy/systemd/autostop-telegram.service"
    unit_path="/etc/systemd/system/autostop-telegram.service"
    venv_root="/opt/autostop-telegram-venv"
    ;;
  work)
    service_user="autostop-work-telegram"
    state_dir="/var/lib/autostop-work-telegram"
    config_dir="/etc/autostop-work-telegram"
    unit_source="${PROJECT_ROOT}/deploy/systemd/autostop-work-telegram.service"
    unit_path="/etc/systemd/system/autostop-work-telegram.service"
    venv_root="/opt/autostop-work-telegram-venv"
    ;;
  *)
    echo "account_invalid=true" >&2
    exit 2
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${temporary_manifest}" && -f "${temporary_manifest}" && ! -L "${temporary_manifest}" ]]; then
    unlink -- "${temporary_manifest}"
  fi
  if [[ -n "${wheelhouse_staging}" && -f "${wheelhouse_staging}" && ! -L "${wheelhouse_staging}" ]]; then
    unlink -- "${wheelhouse_staging}"
  fi
  if [[ "${wheel_staging_dir}" == /var/tmp/autostop-telegram-pyaes-wheel.* ]] \
    && [[ -d "${wheel_staging_dir}" && ! -L "${wheel_staging_dir}" ]]; then
    find -P "${wheel_staging_dir}" -mindepth 1 -delete
    rmdir -- "${wheel_staging_dir}"
  fi
}
trap cleanup EXIT

validate_release_sources() {
  local remote_revision head_revision worktree_status
  release_revision="$(git -C "${PROJECT_ROOT}" rev-parse "${requested_revision}^{commit}")" || return 1
  remote_revision="$(git -C "${PROJECT_ROOT}" rev-parse "origin/${BRANCH}")" || return 1
  head_revision="$(git -C "${PROJECT_ROOT}" rev-parse HEAD)" || return 1
  [[ "${release_revision}" == "${remote_revision}" && "${release_revision}" == "${head_revision}" ]] || return 1
  git -C "${PROJECT_ROOT}" diff --quiet --ignore-submodules || return 1
  git -C "${PROJECT_ROOT}" diff --cached --quiet --ignore-submodules || return 1
  worktree_status="$(git -C "${PROJECT_ROOT}" status --porcelain --untracked-files=all)"
  [[ -z "${worktree_status}" ]]
}

install_model_manifest() {
  local expected_payloads=(config.json model.bin tokenizer.json vocabulary.txt)
  local -a actual_payloads=()
  local payload
  [[ -f "${model_manifest_source}" && ! -L "${model_manifest_source}" ]] || return 1
  mapfile -t actual_payloads < <(awk '/^[0-9a-f]{64}  / { print $2 }' "${model_manifest_source}")
  [[ "${#actual_payloads[@]}" -eq "${#expected_payloads[@]}" ]] || return 1
  for payload in "${!expected_payloads[@]}"; do
    [[ "${actual_payloads[${payload}]}" == "${expected_payloads[${payload}]}" ]] || return 1
  done
  if [[ -L "${model_manifest_dir}" || ( -e "${model_manifest_dir}" && ! -d "${model_manifest_dir}" ) ]]; then
    return 1
  fi
  install -d -m 0700 -o root -g root "${model_manifest_dir}"
  [[ "$(stat -c '%U:%G:%a' "${model_manifest_dir}")" == "root:root:700" ]] || return 1
  if [[ -L "${model_manifest_path}" || ( -e "${model_manifest_path}" && ! -f "${model_manifest_path}" ) ]]; then
    return 1
  fi
  temporary_manifest="$(mktemp "${model_manifest_dir}/.faster-whisper-small.XXXXXX")"
  cp --no-dereference -- "${model_manifest_source}" "${temporary_manifest}"
  chown root:root "${temporary_manifest}"
  chmod 0600 "${temporary_manifest}"
  mv -T -- "${temporary_manifest}" "${model_manifest_path}"
  temporary_manifest=""
}

validate_pyaes_wheel() {
  [[ -f "${pyaes_wheel_path}" && ! -L "${pyaes_wheel_path}" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "${pyaes_wheel_path}")" == "root:root:644" ]] || return 1
  [[ "$(sha256sum -- "${pyaes_wheel_path}" | awk '{print $1}')" == "${pyaes_wheel_sha256}" ]] || return 1
}

build_pyaes_wheelhouse() {
  local staged_source_lock staged_wheel
  if [[ -e "${pyaes_wheel_path}" || -L "${pyaes_wheel_path}" ]]; then
    validate_pyaes_wheel
    return
  fi
  if [[ -L "${wheelhouse_root}" || ( -e "${wheelhouse_root}" && ! -d "${wheelhouse_root}" ) ]]; then
    return 1
  fi
  install -d -m 0755 -o root -g root "${wheelhouse_root}"
  [[ "$(stat -c '%U:%G:%a' "${wheelhouse_root}")" == "root:root:755" ]] || return 1
  wheel_staging_dir="$(mktemp -d /var/tmp/autostop-telegram-pyaes-wheel.XXXXXX)"
  chown "${service_user}:${service_user}" "${wheel_staging_dir}"
  chmod 0700 "${wheel_staging_dir}"
  staged_source_lock="${wheel_staging_dir}/pyaes-source.lock"
  cp --no-dereference -- "${pyaes_source_lock}" "${staged_source_lock}"
  chown "${service_user}:${service_user}" "${staged_source_lock}"
  chmod 0600 "${staged_source_lock}"
  sudo -u "${service_user}" env PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 SOURCE_DATE_EPOCH=0 \
    "${venv_root}/bin/python" -m pip wheel --require-hashes --no-deps --no-build-isolation \
    --wheel-dir "${wheel_staging_dir}/out" -r "${staged_source_lock}"
  staged_wheel="${wheel_staging_dir}/out/${pyaes_wheel_name}"
  [[ -d "${wheel_staging_dir}/out" && ! -L "${wheel_staging_dir}/out" ]] || return 1
  [[ "$(find -P "${wheel_staging_dir}/out" -maxdepth 1 -type f | wc -l)" -eq 1 ]] || return 1
  [[ ! -L "${staged_wheel}" && -f "${staged_wheel}" ]] || return 1
  [[ "$(sha256sum -- "${staged_wheel}" | awk '{print $1}')" == "${pyaes_wheel_sha256}" ]] || return 1
  wheelhouse_staging="$(mktemp "${wheelhouse_root}/.pyaes-1.6.1.XXXXXX")"
  cp --no-dereference -- "${staged_wheel}" "${wheelhouse_staging}"
  chown root:root "${wheelhouse_staging}"
  chmod 0644 "${wheelhouse_staging}"
  [[ "$(sha256sum -- "${wheelhouse_staging}" | awk '{print $1}')" == "${pyaes_wheel_sha256}" ]] || return 1
  mv -T -- "${wheelhouse_staging}" "${pyaes_wheel_path}"
  wheelhouse_staging=""
  validate_pyaes_wheel
}

if [[ ! -f "${requirements_lock}" || -L "${requirements_lock}" \
  || ! -f "${build_requirements_lock}" || -L "${build_requirements_lock}" \
  || ! -f "${pyaes_source_lock}" || -L "${pyaes_source_lock}" \
  || ! -f "${pyaes_wheel_lock}" || -L "${pyaes_wheel_lock}" ]]; then
  echo "telegram_dependency_lock_invalid=true" >&2
  exit 1
fi
if ! validate_release_sources; then
  echo "telegram_release_source_invalid=true" >&2
  exit 1
fi
if ! install_model_manifest; then
  echo "transcription_model_manifest_invalid=true" >&2
  exit 1
fi

if ! id "${service_user}" >/dev/null 2>&1; then
  useradd --system --home-dir "${state_dir}" --shell /usr/sbin/nologin "${service_user}"
fi

install -d -m 0700 -o "${service_user}" -g "${service_user}" "${state_dir}"
if [[ "${account}" == "work" ]]; then
  if [[ -L "${config_dir}" || ( -e "${config_dir}" && ! -d "${config_dir}" ) ]]; then
    echo "work_config_directory_invalid=true" >&2
    exit 1
  fi
  install -d -m 0710 -o root -g "${service_user}" "${config_dir}"
  if [[ "$(stat -c '%U:%G:%a' "${config_dir}")" != "root:${service_user}:710" ]]; then
    echo "work_config_directory_permissions_invalid=true" >&2
    exit 1
  fi
else
  install -d -m 0700 -o "${service_user}" -g "${service_user}" "${config_dir}"
fi

if [[ "${account}" == "work" ]]; then
  target_credentials="${config_dir}/credentials"
  if [[ -L "${target_credentials}" || ( -e "${target_credentials}" && ! -f "${target_credentials}" ) ]]; then
    echo "work_credentials_invalid=true" >&2
    exit 1
  fi
  if [[ ! -e "${target_credentials}" ]]; then
    source_credentials="/etc/autostop-telegram/credentials"
    if [[ ! -f "${source_credentials}" || -L "${source_credentials}" ]]; then
      echo "source_credentials_present=false" >&2
      exit 1
    fi
    if [[ "$(stat -c '%U:%G:%a' "${source_credentials}")" != "autostop-telegram:autostop-telegram:600" ]]; then
      echo "source_credentials_permissions_invalid=true" >&2
      exit 1
    fi
    temporary_credentials="$(mktemp "${config_dir}/.credentials.XXXXXX")"
    if ! cp --no-dereference -- "${source_credentials}" "${temporary_credentials}"; then
      rm -f -- "${temporary_credentials}"
      echo "work_credentials_copy_failed=true" >&2
      exit 1
    fi
    if [[ -L "${temporary_credentials}" || ! -f "${temporary_credentials}" ]]; then
      rm -f -- "${temporary_credentials}"
      echo "work_credentials_copy_invalid=true" >&2
      exit 1
    fi
    chown "${service_user}:${service_user}" "${temporary_credentials}"
    chmod 0600 "${temporary_credentials}"
    mv -T -- "${temporary_credentials}" "${target_credentials}"
  fi
fi

if [[ -f "${config_dir}/credentials" ]] \
  && [[ "$(stat -c '%U:%G:%a' "${config_dir}/credentials")" != "${service_user}:${service_user}:600" ]]; then
  echo "credentials_permissions_invalid=true" >&2
  exit 1
fi

if [[ ! -x "${venv_root}/bin/python" ]]; then
  python3 -m venv "${venv_root}"
fi
if [[ "$("${venv_root}/bin/python" -c 'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}:{platform.machine()}")')" != "3.12:x86_64" ]]; then
  echo "telegram_dependency_platform_unsupported=true" >&2
  exit 1
fi
"${venv_root}/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps \
  -r "${build_requirements_lock}"
chmod -R a+rX "${venv_root}"
if ! build_pyaes_wheelhouse; then
  echo "telegram_pyaes_wheelhouse_invalid=true" >&2
  exit 1
fi
"${venv_root}/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps --no-index \
  --find-links "${wheelhouse_root}" -r "${pyaes_wheel_lock}"
"${venv_root}/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps --only-binary=:all: \
  -r "${requirements_lock}"
if ! "${venv_root}/bin/python" -m pip check; then
  echo "telegram_dependency_check_failed=true" >&2
  exit 1
fi
if [[ "${account}" == "work" ]]; then
  chown -R root:root "${venv_root}"
  chmod -R go-w "${venv_root}"
fi
chmod -R a+rX "${venv_root}"

install -m 0644 "${unit_source}" "${unit_path}"
systemctl daemon-reload

echo "telegram_bridge_installed=true"
echo "account=${account}"
echo "credentials_present=$([[ -f "${config_dir}/credentials" ]] && echo true || echo false)"
