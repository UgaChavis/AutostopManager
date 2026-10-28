#!/usr/bin/env bash
set -Eeuo pipefail

active_root="$(/usr/bin/readlink -f /opt/autostop-manager-releases/current)"
revision="$(/usr/bin/bash "${active_root}/scripts/run-j1-browser-stack.sh" revision)"
export AUTOSTOP_J1_BROWSER_IMAGE_REVISION="${revision}"
exec /usr/bin/docker compose \
    -f "${active_root}/deploy/j1-browser/docker-compose.yml" \
    -f /etc/autostop-j1-browser/20261028/resource-overlay.yaml \
    --project-name autostop-j1-browser \
    up --detach --no-build --remove-orphans
