#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo 'run_as_root_required=true' >&2; exit 1; }
[[ $# -eq 0 ]] || { echo "usage: $0" >&2; exit 2; }
release="$(readlink -f /opt/autostop-work-telegram-releases/current)"
unit=/etc/systemd/system/autostop-codex-wake.service
boot_unit=/etc/systemd/system/autostop-codex-start.service
python=/opt/AutostopManager/.venv/bin/python
[[ "$release" == /opt/autostop-work-telegram-releases/* && -d "$release" && ! -L "$release" ]]
[[ "$(stat -c %U "$release")" == root ]]
! systemctl is-active --quiet autostop-codex-wake.service
export PYTHONPATH="$release" PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
"$python" -c 'import websockets; assert websockets.__version__ == "15.0.1"'
/usr/local/bin/codex app-server daemon version | "$python" -c 'import json,sys; s=json.load(sys.stdin); sys.exit(not(s.get("status")=="running" and s.get("cliVersion")==s.get("appServerVersion")))'
systemd-analyze verify "$release/deploy/systemd/autostop-codex-start.service" "$release/deploy/systemd/autostop-codex-wake.service"
umask 077
backup="$(mktemp -d /etc/autostop-work-telegram/wake-rollback.XXXXXX)"
[[ ! -f "$unit" ]] || cp -a "$unit" "$backup/unit"
[[ ! -f "$boot_unit" ]] || cp -a "$boot_unit" "$backup/boot-unit"
[[ ! -f /etc/autostop-work-telegram/wake.json ]] || cp -a /etc/autostop-work-telegram/wake.json "$backup/config"
restore() {
  local code=$?
  if [[ -f "$backup/unit" ]]; then cp -a "$backup/unit" "$unit"; else unlink "$unit" 2>/dev/null || true; fi
  if [[ -f "$backup/boot-unit" ]]; then cp -a "$backup/boot-unit" "$boot_unit"; else unlink "$boot_unit" 2>/dev/null || true; fi
  if [[ -f "$backup/config" ]]; then cp -a "$backup/config" /etc/autostop-work-telegram/wake.json; fi
  systemctl daemon-reload
  return "$code"
}
trap restore ERR
"$python" -m autostop_manager.telegram_wake setup
install -o root -g root -m 0644 "$release/deploy/systemd/autostop-codex-wake.service" "$unit"
install -o root -g root -m 0644 "$release/deploy/systemd/autostop-codex-start.service" "$boot_unit"
systemctl daemon-reload
systemctl enable --now autostop-codex-start.service
trap - ERR
printf '%s\n' 'codex_wake_installed=true' 'activation=paused'
