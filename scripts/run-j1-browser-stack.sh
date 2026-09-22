#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
REVISION_FILE="${RELEASE_ROOT}/REVISION"
COMPOSE_FILE="${RELEASE_ROOT}/deploy/j1-browser/docker-compose.yml"
PROJECT_NAME="autostop-j1-browser"

usage() {
  echo "usage: $0 {start|stop|revision}" >&2
}

action="${1:-}"
if [[ $# -ne 1 ]] || [[ "${action}" != "start" && "${action}" != "stop" && "${action}" != "revision" ]]; then
  usage
  exit 2
fi

if [[ "${EUID}" -ne 0 ]] \
  || [[ ! -d "${RELEASE_ROOT}" ]] \
  || [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]] \
  || [[ ! -f "${REVISION_FILE}" || -L "${REVISION_FILE}" ]] \
  || [[ ! -f "${COMPOSE_FILE}" || -L "${COMPOSE_FILE}" ]]; then
  echo "j1_browser_active_release_required=true" >&2
  exit 1
fi

read -r revision_owner revision_mode revision_size < <(stat -c '%u %a %s' "${REVISION_FILE}")
if [[ ! "${revision_owner}" =~ ^[0-9]+$ ]] \
  || [[ ! "${revision_mode}" =~ ^[0-7]{3,4}$ ]] \
  || [[ ! "${revision_size}" =~ ^[0-9]+$ ]] \
  || (( revision_owner != 0 )) \
  || (( revision_size > 80 )) \
  || (( (8#${revision_mode} & 8#022) != 0 )); then
  echo "j1_browser_release_revision_invalid=true" >&2
  exit 1
fi

mapfile -t revision_lines < "${REVISION_FILE}"
if [[ "${#revision_lines[@]}" -ne 1 ]] || [[ ! "${revision_lines[0]}" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "j1_browser_release_revision_invalid=true" >&2
  exit 1
fi
revision="${revision_lines[0]}"

if [[ "${action}" == "revision" ]]; then
  printf '%s\n' "${revision}"
  exit 0
fi
if [[ ! -x /usr/bin/docker ]]; then
  echo "j1_browser_docker_required=true" >&2
  exit 1
fi

export AUTOSTOP_J1_BROWSER_IMAGE_REVISION="${revision}"
if [[ "${action}" == "start" ]]; then
  exec /usr/bin/docker compose -f "${COMPOSE_FILE}" --project-name "${PROJECT_NAME}" \
    up --detach --no-build --remove-orphans
fi
exec /usr/bin/docker compose -f "${COMPOSE_FILE}" --project-name "${PROJECT_NAME}" \
  down --remove-orphans
