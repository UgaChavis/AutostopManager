"""Disposable shell commands and paths for work-Telegram duty tests."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def duty_fixture(tmp_path, state, *, inbound=True, bridge_available=True, bypass_root=True):
    releases = tmp_path / "releases"
    release = releases / "revision"
    release.mkdir(parents=True)
    current = releases / "current"
    current.symlink_to(release)
    monitor = tmp_path / "monitor.env"
    intent = (
        "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n"
        "AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET=/run/autostop-codex-wake/wake.sock\n"
    )
    if inbound:
        monitor.write_text(intent)
    config = tmp_path / "wake.json"
    config.write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    mocks = {
        "wake-python": '#!/bin/sh\nprintf "%s\\n" "$WAKE_STATE"\n',
        "systemctl": (
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n'
            'if [ "$*" = "is-active --quiet autostop-codex-wake.service" ]; then\n'
            '  [ "$WAKE_ACTIVE" = 1 ]; exit $?\nfi\nexit 0\n'
        ),
        "sudo": ('#!/bin/sh\n[ "$BRIDGE_AVAILABLE" = 1 ] || exit 1\nprintf "%s\\n" "$BRIDGE_STATE"\n'),
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    if bypass_root:
        mocks.update(stat='#!/bin/sh\nprintf "%s\\n" root:root:644\n', chown="#!/bin/sh\nexit 0\n")
    for name, text in mocks.items():
        path = bin_dir / name
        path.write_text(text)
        path.chmod(0o755)
    source = (ROOT / "scripts/set-work-telegram-duty.sh").read_text()
    replacements = {
        'release_link="/opt/autostop-work-telegram-releases/current"': f'release_link="{current}"',
        'venv_python="/opt/autostop-work-telegram-venv/bin/python"': f'venv_python="{sys.executable}"',
        'wake_python="/opt/AutostopManager/.venv/bin/python"': f'wake_python="{bin_dir / "wake-python"}"',
        'wake_config="/etc/autostop-work-telegram/wake.json"': f'wake_config="{config}"',
        'monitor_env="/etc/autostop-work-telegram/monitor.env"': f'monitor_env="{monitor}"',
        'control_lock="/run/autostop-work-telegram-control.lock"': f'control_lock="{tmp_path / "lock"}"',
        "/opt/autostop-work-telegram-releases/*": f"{releases}/*",
    }
    # Behavioral tests may run as non-root; root-gate integration tests keep it.
    if bypass_root:
        replacements['if [[ "${EUID}" -ne 0 ]]; then'] = "if false; then"
    for old, new in replacements.items():
        assert old in source
        source = source.replace(old, new, 1)
    script = tmp_path / "duty.sh"
    script.write_text(source)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "WAKE_STATE": json.dumps(state),
        "WAKE_ACTIVE": "1" if inbound else "0",
        "BRIDGE_AVAILABLE": "1" if bridge_available else "0",
        "BRIDGE_STATE": json.dumps(
            {
                "ok": True,
                "transport_ready": True,
                "inbound_enabled": inbound,
                "owner_notification_configured": True,
                "enabled": inbound,
                "retention": "memory_only",
            }
        ),
    }
    return script, env, monitor, intent, log
