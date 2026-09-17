#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
UNIT_NAME="autostop-j1-browser.service"
UNIT_SOURCE="${PROJECT_ROOT}/deploy/systemd/${UNIT_NAME}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
COMPOSE_FILE="${PROJECT_ROOT}/deploy/j1-browser/docker-compose.yml"
SOCKET_DIR="/run/autostop-j1-browser"
ISOLATION_MARKER="${SOCKET_DIR}/isolation-ready"
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

if [[ "${EUID}" -ne 0 ]] || ! command -v systemctl >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
  echo "j1_browser_root_systemd_docker_required=true" >&2
  exit 1
fi
if [[ ! -d "${RELEASE_ROOT}" ]] || [[ "$(readlink -f "${PROJECT_ROOT}")" != "$(readlink -f "${RELEASE_ROOT}")" ]]; then
  echo "j1_browser_current_release_source_required=true" >&2
  exit 1
fi
if [[ ! -f "${UNIT_SOURCE}" || -L "${UNIT_SOURCE}" || ! -f "${COMPOSE_FILE}" || -L "${COMPOSE_FILE}" ]]; then
  echo "j1_browser_source_invalid=true" >&2
  exit 1
fi
if [[ -L "${UNIT_PATH}" || ( -e "${UNIT_PATH}" && ! -f "${UNIT_PATH}" ) ]]; then
  echo "j1_browser_installed_unit_invalid=true" >&2
  exit 1
fi
if [[ -f "${UNIT_PATH}" && "${replace_unit}" -ne 1 ]] && ! cmp -s "${UNIT_SOURCE}" "${UNIT_PATH}"; then
  echo "j1_browser_replace_unit_required=true" >&2
  exit 1
fi

docker compose -f "${COMPOSE_FILE}" --project-name autostop-j1-browser config --quiet
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNIT_PATH}"
fi
echo "j1_browser_unit_installed=true"
rm -f -- "${ISOLATION_MARKER}"
echo "j1_browser_isolation_attestation_required=true"

if [[ "${activate}" -ne 1 ]]; then
  exit 0
fi

systemctl enable "${UNIT_NAME}"
if systemctl is-active --quiet "${UNIT_NAME}"; then
  systemctl restart "${UNIT_NAME}"
else
  systemctl start "${UNIT_NAME}"
fi

for _attempt in {1..50}; do
  if [[ -S "${SOCKET_DIR}/renderer.sock" ]] && systemctl is-active --quiet "${UNIT_NAME}"; then
    echo "j1_browser_active=true"
    exit 0
  fi
  sleep 0.2
done
echo "j1_browser_socket_unavailable=true" >&2
exit 1
