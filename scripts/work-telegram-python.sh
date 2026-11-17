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
command="$(timeout 10 systemctl show --property=ExecStart --value "${unit}")" || exit 1
if [[ "${command}" =~ ^\{\ path=(/[^\;[:space:]]+)\ \; ]]; then
  python="${BASH_REMATCH[1]}"
  if [[ -x "${python}" && "${command}" == *" -m ${module} "* ]]; then
    printf '%s\n' "${python}"
    exit 0
  fi
fi
echo "${error_prefix}_runtime_unavailable=true" >&2
exit 1
