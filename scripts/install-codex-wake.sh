#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo 'run_as_root_required=true' >&2; exit 1; }
no_start=0
if [[ $# -eq 1 && "$1" == "--no-start" ]]; then
  no_start=1
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--no-start]" >&2; exit 2
fi
release="$(readlink -f /opt/autostop-work-telegram-releases/current)"
unit=/etc/systemd/system/autostop-codex-wake.service
boot_unit=/etc/systemd/system/autostop-codex-start.service
python=/opt/AutostopManager/.venv/bin/python
[[ ( "$release" == /opt/autostop-work-telegram-releases/* || "$release" == /opt/autostop-manager-releases/* ) \
  && -d "$release" && ! -L "$release" ]]
[[ "$(stat -c %U "$release")" == root ]]
[[ -f "$release/REVISION" && ! -L "$release/REVISION" \
  && -f "$release/MANIFEST.sha256" && ! -L "$release/MANIFEST.sha256" ]]
if [[ "$no_start" -eq 1 ]]; then
  load="$(systemctl show --property=LoadState --value autostop-codex-wake.service)"
  state="$(systemctl show --property=ActiveState --value autostop-codex-wake.service)"
  [[ "$load" == not-found || ( "$load" == loaded && "$state" == inactive ) ]] || {
    echo "codex_wake_service_must_be_inactive=true" >&2; exit 1;
  }
elif systemctl is-active --quiet autostop-codex-wake.service; then
  echo "codex_wake_service_must_be_inactive=true" >&2
  exit 1
fi
export PYTHONPATH="$release" PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
"$python" -c 'import websockets; assert websockets.__version__ == "15.0.1"'
if [[ "$no_start" -eq 0 ]]; then
  /usr/local/bin/codex app-server daemon version | "$python" -c 'import json,sys; s=json.load(sys.stdin); sys.exit(not(s.get("status")=="running" and s.get("cliVersion")==s.get("appServerVersion")))'
fi
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
  if [[ -f "$backup/config" ]]; then cp -a "$backup/config" /etc/autostop-work-telegram/wake.json; else unlink /etc/autostop-work-telegram/wake.json 2>/dev/null || true; fi
  systemctl daemon-reload
  return "$code"
}
trap restore ERR
if [[ "$no_start" -eq 0 ]]; then
  "$python" -m autostop_manager.telegram_wake setup
fi
install -o root -g root -m 0644 "$release/deploy/systemd/autostop-codex-wake.service" "$unit"
install -o root -g root -m 0644 "$release/deploy/systemd/autostop-codex-start.service" "$boot_unit"
systemctl daemon-reload
if [[ "$no_start" -eq 0 ]]; then
  systemctl enable --now autostop-codex-start.service
fi
trap - ERR
printf '%s\n' 'codex_wake_installed=true' 'activation=paused'
