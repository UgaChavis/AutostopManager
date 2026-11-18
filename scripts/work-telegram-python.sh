#!/usr/bin/env bash
# Use systemd's effective interpreter, including deployment drop-ins.
set -Eeuo pipefail
error_prefix=work_telegram
case "${1:-}" in
  personal) unit=autostop-telegram.service; module=autostop_manager.telegram_bridge; error_prefix=personal_telegram ;;
  bridge) unit=autostop-work-telegram.service; module=autostop_manager.telegram_bridge ;;
  wake) unit=autostop-codex-wake.service; module=autostop_manager.telegram_wake ;;
  *) echo 'work_telegram_runtime_invalid=true' >&2; exit 2 ;;
esac
release_dir=""
if [[ $# -ne 1 ]]; then
  if [[ "$#" -lt 5 || "$1" != bridge || "$2" != --run || ! "$3" =~ ^[0-9a-f]{40}$ \
    || "$4" != -- || ! "$5" =~ ^[a-z][a-z0-9-]*$ ]]; then
    echo 'work_telegram_runtime_invalid=true' >&2
    exit 2
  fi
  expected_revision="$3"
  shift 4
  script_path="$(readlink -e -- "${BASH_SOURCE[0]}" 2>/dev/null || true)"
  release_dir="${script_path%/scripts/work-telegram-python.sh}"
  release_parent="${release_dir%/*}"
  package_dir="${release_dir}/autostop_manager"
  if [[ "${script_path}" != "${release_dir}/scripts/work-telegram-python.sh" || "${release_dir}" == *:* \
    || ( "${release_parent}" != /opt/autostop-work-telegram-releases \
      && "${release_parent}" != /opt/autostop-manager-releases ) \
    || ! -f "${release_dir}/REVISION" || -L "${release_dir}/REVISION" \
    || "$(<"${release_dir}/REVISION")" != "${expected_revision}" \
    || -L "${package_dir}" || ! -f "${package_dir}/__init__.py" || -L "${package_dir}/__init__.py" \
    || ! -f "${package_dir}/telegram_bridge.py" || -L "${package_dir}/telegram_bridge.py" ]]; then
    echo 'work_telegram_release_mismatch=true' >&2
    exit 1
  fi
fi
command="$(timeout 10 systemctl show --property=ExecStart --value "${unit}")" || exit 1
if [[ "${command}" =~ ^\{\ path=(/[^\;[:space:]]+)\ \; ]]; then
  python="${BASH_REMATCH[1]}"
  if [[ -x "${python}" && "${command}" == *" -m ${module} "* ]]; then
    if [[ -n "${release_dir}" ]]; then
      exec /usr/bin/env -u PYTHONHOME \
        PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 "PYTHONPATH=${release_dir}" \
        "${python}" -B -P -m "${module}" --account work "$@"
    fi
    printf '%s\n' "${python}"
    exit 0
  fi
fi
echo "${error_prefix}_runtime_unavailable=true" >&2
exit 1
