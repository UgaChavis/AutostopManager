#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
RUNTIME_PYTHON="/usr/bin/python3"
SOCKET_PATH="/run/autostop-j1-browser/renderer.sock"

if [[ "${EUID}" -ne 0 ]] \
  || [[ ! -x "${RUNTIME_PYTHON}" ]] \
  || [[ ! -d "${RELEASE_ROOT}" ]] \
  || [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]]; then
  echo "j1_browser_active_release_required=true" >&2
  exit 1
fi

for _attempt in {1..50}; do
  if [[ -S "${SOCKET_PATH}" ]]; then
    break
  fi
  sleep 0.2
done
if [[ ! -S "${SOCKET_PATH}" ]]; then
  echo "j1_browser_socket_unavailable=true" >&2
  exit 1
fi

exec env \
  PYTHONPATH="${RELEASE_ROOT}" \
  PYTHONSAFEPATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  "${RUNTIME_PYTHON}" -m autostop_manager.j1_browser_verify attest
