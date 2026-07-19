#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_ROOT="$PROJECT_ROOT/deploy/systemd"

install -d -m 0700 /var/lib/autostop-manager/integration
install -m 0644 "$UNIT_ROOT/autostop-integration-audit.service" /etc/systemd/system/
install -m 0644 "$UNIT_ROOT/autostop-integration-audit.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autostop-integration-audit.timer
