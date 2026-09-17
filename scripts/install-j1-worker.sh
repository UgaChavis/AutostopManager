#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
RUNTIME_PYTHON="/usr/bin/python3"
UNIT_NAME="autostop-j1.service"
UNIT_SOURCE="${PROJECT_ROOT}/deploy/systemd/${UNIT_NAME}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
activate=0
replace_unit=0

usage() {
  echo "usage: $0 [--activate] [--replace-unit]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) activate=1 ;;
    --replace-unit) replace_unit=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

if [[ "${EUID}" -ne 0 ]] || ! command -v systemctl >/dev/null 2>&1; then
  echo "j1_root_systemd_required=true" >&2
  exit 1
fi
if [[ ! -d "${RELEASE_ROOT}" ]] \
  || [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]]; then
  echo "j1_current_release_source_required=true" >&2
  exit 1
fi
if [[ ! -x "${RUNTIME_PYTHON}" || ! -f "${UNIT_SOURCE}" || -L "${UNIT_SOURCE}" ]]; then
  echo "j1_runtime_prerequisite_invalid=true" >&2
  exit 1
fi
if [[ -L "${UNIT_PATH}" || ( -e "${UNIT_PATH}" && ! -f "${UNIT_PATH}" ) ]]; then
  echo "j1_installed_unit_invalid=true" >&2
  exit 1
fi
if [[ -f "${UNIT_PATH}" && "${replace_unit}" -ne 1 ]] \
  && ! cmp -s "${UNIT_SOURCE}" "${UNIT_PATH}"; then
  echo "j1_replace_unit_required=true" >&2
  exit 1
fi

install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNIT_PATH}"
fi
echo "j1_unit_installed=true"

if [[ "${activate}" -ne 1 ]]; then
  exit 0
fi

systemctl enable "${UNIT_NAME}"
if systemctl is-active --quiet "${UNIT_NAME}"; then
  systemctl restart "${UNIT_NAME}"
else
  systemctl start "${UNIT_NAME}"
fi

# A successful start alone can precede an immediate worker crash. Require the
# service to remain running, then probe the same release and cache it uses.
sleep 1
systemctl is-active --quiet "${UNIT_NAME}"
env \
  PYTHONPATH="${RELEASE_ROOT}" \
  PYTHONSAFEPATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1 \
  AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890 \
  "${RUNTIME_PYTHON}" -m autostop_manager.j1_research probe
systemctl is-active --quiet "${UNIT_NAME}"
echo "j1_worker_active=true"
