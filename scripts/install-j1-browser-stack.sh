#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RELEASE_ROOT="/opt/autostop-manager-releases/current"
RUNTIME_PYTHON="/usr/bin/python3"
UNIT_NAME="autostop-j1-browser.service"
UNIT_SOURCE="${PROJECT_ROOT}/deploy/systemd/${UNIT_NAME}"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
COMPOSE_FILE="${PROJECT_ROOT}/deploy/j1-browser/docker-compose.yml"
DOCKERFILE="${PROJECT_ROOT}/deploy/j1-browser/Dockerfile"
STACK_RUNNER="${PROJECT_ROOT}/scripts/run-j1-browser-stack.sh"
ATTESTER="${PROJECT_ROOT}/scripts/attest-j1-browser-stack.sh"
SOCKET_DIR="/run/autostop-j1-browser"
ATTESTATION_DIR="/run/autostop-j1-browser-attestation"
ISOLATION_MARKER="${ATTESTATION_DIR}/isolation-ready"
VERIFIER_MODULE="autostop_manager.j1_browser_verify"
activate=0
verify=0
replace_unit=0

usage() {
  echo "usage: $0 [--activate] [--verify] [--replace-unit]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) activate=1 ;;
    --verify) verify=1 ;;
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
if [[ ! -x "${RUNTIME_PYTHON}" || ! -f "${UNIT_SOURCE}" || -L "${UNIT_SOURCE}" \
  || ! -f "${COMPOSE_FILE}" || -L "${COMPOSE_FILE}" \
  || ! -f "${DOCKERFILE}" || -L "${DOCKERFILE}" \
  || ! -x "${STACK_RUNNER}" || -L "${STACK_RUNNER}" \
  || ! -x "${ATTESTER}" || -L "${ATTESTER}" \
  || ! -f "${PROJECT_ROOT}/autostop_manager/j1_browser_verify.py" \
  || -L "${PROJECT_ROOT}/autostop_manager/j1_browser_verify.py" ]]; then
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

revision="$("${STACK_RUNNER}" revision)"
renderer_image="autostop-j1-browser-renderer:${revision}"
proxy_image="autostop-j1-browser-proxy:${revision}"
AUTOSTOP_J1_BROWSER_IMAGE_REVISION="${revision}" \
  docker compose -f "${COMPOSE_FILE}" --project-name autostop-j1-browser config --quiet
if [[ "${activate}" -eq 1 ]]; then
  # Build before replacing/starting the unit.  Keeping image construction out
  # of systemd makes later boot and restart deterministic and bounded.
  AUTOSTOP_J1_BROWSER_IMAGE_REVISION="${revision}" \
    docker compose -f "${COMPOSE_FILE}" --project-name autostop-j1-browser build
  docker image inspect "${renderer_image}" "${proxy_image}" >/dev/null
  for image in "${renderer_image}" "${proxy_image}"; do
    image_revision="$(docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${image}")"
    if [[ "${image_revision}" != "${revision}" ]]; then
      echo "j1_browser_image_revision_invalid=true" >&2
      exit 1
    fi
  done
  echo "j1_browser_images_built=true"
fi
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNIT_PATH}"
fi
echo "j1_browser_unit_installed=true"
rm -f -- "${ISOLATION_MARKER}"
echo "j1_browser_isolation_attestation_required=true"

fail_closed_readback() {
  local reason="$1"
  # The marker is an authorization signal, not just health metadata.  Never
  # retain one created during this run when the attester/readback does not
  # complete successfully.
  rm -f -- "${ISOLATION_MARKER}"
  echo "${reason}=true" >&2
  exit 1
}

if [[ "${activate}" -ne 1 ]]; then
  if [[ "${verify}" -ne 1 ]]; then
    exit 0
  fi
else
  systemctl enable "${UNIT_NAME}"
  if systemctl is-active --quiet "${UNIT_NAME}"; then
    systemctl restart "${UNIT_NAME}"
  else
    systemctl start "${UNIT_NAME}"
  fi

  for _attempt in {1..50}; do
    if [[ -S "${SOCKET_DIR}/renderer.sock" ]] && systemctl is-active --quiet "${UNIT_NAME}"; then
      break
    fi
    sleep 0.2
  done
  if [[ ! -S "${SOCKET_DIR}/renderer.sock" ]] || ! systemctl is-active --quiet "${UNIT_NAME}"; then
    fail_closed_readback "j1_browser_socket_unavailable"
  fi
fi

if ! systemctl is-active --quiet "${UNIT_NAME}"; then
  fail_closed_readback "j1_browser_service_inactive"
fi
if [[ "${activate}" -ne 1 ]] && ! "${ATTESTER}"; then
  fail_closed_readback "j1_browser_attestation_failed"
fi
if ! probe_json="$(env \
  PYTHONPATH="${RELEASE_ROOT}" \
  PYTHONSAFEPATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  "${RUNTIME_PYTHON}" -m "${VERIFIER_MODULE}" probe)"; then
  fail_closed_readback "j1_browser_readback_failed"
fi
if ! "${RUNTIME_PYTHON}" -c \
  'import json, sys; p=json.load(sys.stdin); raise SystemExit(0 if p.get("ok") is True and p.get("browser_ready") is True and p.get("browser_containers_ready") is True else 1)' \
  <<<"${probe_json}"; then
  fail_closed_readback "j1_browser_readback_failed"
fi
if ! systemctl is-active --quiet "${UNIT_NAME}"; then
  fail_closed_readback "j1_browser_service_inactive"
fi
echo "j1_browser_active=true"
echo "j1_browser_isolation_attested=true"
