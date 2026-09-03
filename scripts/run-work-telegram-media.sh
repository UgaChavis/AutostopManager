#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run_as_root_required=true" >&2
  exit 1
fi
if [[ $# -lt 3 ]]; then
  echo "usage: $0 transcribe|preview --file /run/autostop-work-telegram/inbox/EXACT_FILE [--language ru] [--delete-after]" >&2
  exit 2
fi

action="$1"
shift
inbox_dir="/run/autostop-work-telegram/inbox"
release_root="/opt/autostop-work-telegram-releases/current"
venv_python="/opt/autostop-work-telegram-venv/bin/python"
service_user="autostop-work-telegram"
release_dir=""
file_path=""
language="ru"
delete_after=0

case "${action}" in
  transcribe)
    module="autostop_manager.telegram_transcribe"
    ;;
  preview)
    module="autostop_manager.telegram_video_preview"
    ;;
  *)
    echo "media_action_invalid=true" >&2
    exit 2
    ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)
      [[ -z "${file_path}" && $# -ge 2 ]] || { echo "media_file_invalid=true" >&2; exit 2; }
      file_path="$2"
      shift 2
      ;;
    --language)
      [[ "${action}" == "transcribe" && $# -ge 2 ]] || { echo "media_language_invalid=true" >&2; exit 2; }
      language="$2"
      shift 2
      ;;
    --delete-after)
      [[ "${delete_after}" -eq 0 ]] || { echo "media_delete_after_invalid=true" >&2; exit 2; }
      delete_after=1
      shift
      ;;
    *)
      echo "media_arguments_invalid=true" >&2
      exit 2
      ;;
  esac
done

[[ "${file_path}" == "${inbox_dir}/"* && "$(dirname -- "${file_path}")" == "${inbox_dir}" ]] \
  || { echo "media_file_invalid=true" >&2; exit 1; }
[[ -f "${file_path}" && ! -L "${file_path}" ]] || { echo "media_file_invalid=true" >&2; exit 1; }
case "${action}:${file_path##*.}" in
  transcribe:m4a|transcribe:mp3|transcribe:ogg|transcribe:opus|preview:mp4) ;;
  *)
    echo "media_file_invalid=true" >&2
    exit 1
    ;;
esac
[[ "${language}" =~ ^[a-z]{2,8}$ ]] || { echo "media_language_invalid=true" >&2; exit 2; }
release_dir="$(readlink -f -- "${release_root}" 2>/dev/null || true)"
[[ "${release_dir}" == /opt/autostop-work-telegram-releases/* && -d "${release_dir}" && ! -L "${release_dir}" \
  && -x "${venv_python}" ]] \
  || { echo "media_runtime_unavailable=true" >&2; exit 1; }

command=(
  /usr/bin/env
  "PYTHONDONTWRITEBYTECODE=1"
  "PYTHONPATH=${release_dir}"
  "${venv_python}"
  -m
  "${module}"
  --account
  work
  --file
  "${file_path}"
)
if [[ "${action}" == "transcribe" ]]; then
  command+=(--language "${language}")
fi
if [[ "${delete_after}" -eq 1 ]]; then
  command+=(--delete-after)
fi

systemd-run --quiet --wait --pipe --collect --service-type=exec --unit="autostop-work-telegram-media-$$" \
  --property="User=${service_user}" \
  --property="Group=${service_user}" \
  --property="NoNewPrivileges=true" \
  --property="PrivateNetwork=true" \
  --property="PrivateTmp=true" \
  --property="PrivateDevices=true" \
  --property="ProtectSystem=strict" \
  --property="ProtectHome=true" \
  --property="ProtectKernelTunables=true" \
  --property="ProtectKernelModules=true" \
  --property="ProtectControlGroups=true" \
  --property="ProtectProc=invisible" \
  --property="RestrictSUIDSGID=true" \
  --property="RestrictNamespaces=true" \
  --property="LockPersonality=true" \
  --property="MemoryDenyWriteExecute=true" \
  --property="RestrictAddressFamilies=AF_UNIX" \
  --property="InaccessiblePaths=/var/lib/autostop-work-telegram /etc/autostop-work-telegram /run/autostop-work-telegram/bridge.sock /run/autostop-work-telegram/outbox" \
  --property="ReadWritePaths=${inbox_dir}" \
  -- "${command[@]}"
