from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge
from autostop_manager.telegram_bridge import (
    BridgeError,
    TelegramConfig,
    _ensure_private_key,
    _read_private_download,
    _requires_mutation_lock,
    account_inbox_dir,
    account_model_dir,
    account_outbox_dir,
    build_parser,
    issue_download_contract,
    issue_send_contract,
    issue_photo_contract,
    normalize_phone,
    redact_sensitive_message_text,
    validate_download_request,
    validate_photo_file,
    validate_photo_request,
    validate_send_request,
    verify_download_contract,
    verify_photo_contract,
    verify_send_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_service_uses_dedicated_immutable_telegram_release() -> None:
    service = (ROOT / "deploy/systemd/autostop-telegram.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/autostop-telegram-releases/current" in service
    assert "Environment=PYTHONPATH=/opt/autostop-telegram-releases/current" in service
    assert "--account personal daemon" in service
    assert "/opt/AutostopManager" not in service


def test_work_telegram_service_has_no_personal_state_or_socket() -> None:
    service = (ROOT / "deploy/systemd/autostop-work-telegram.service").read_text(encoding="utf-8")

    assert "User=autostop-work-telegram" in service
    assert "SupplementaryGroups=10001" in service
    assert "WorkingDirectory=/opt/autostop-work-telegram-releases/current" in service
    assert "--account work daemon" in service
    assert "/opt/autostop-work-telegram-venv/bin/python" in service
    assert "EnvironmentFile=-/etc/autostop-work-telegram/owner-notification.env" in service
    assert "EnvironmentFile=-/etc/autostop-work-telegram/monitor.env" in service
    assert "/opt/autostop-telegram-venv/bin/python" not in service
    assert "/var/lib/autostop-work-telegram" in service
    assert "/run/autostop-work-telegram" in service
    assert "/var/lib/autostop-telegram" not in service
    assert "/run/autostop-telegram" not in service


def test_dedicated_telegram_deploy_script_is_syntax_valid_and_scoped() -> None:
    script = ROOT / "scripts/deploy_telegram_bridge.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "origin/${BRANCH}" in text
    assert 'fetch --quiet --prune origin "refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"' in text
    assert "ls-remote --heads origin" in text
    assert "Telegram release checkout must match the fetched remote branch" in text
    assert "status --porcelain --untracked-files=all" in text
    assert 'git -C "${SOURCE_DIR}" archive' in text
    assert "autostop-telegram.service" in text
    assert "autostop-work-telegram.service" in text
    assert "--no-start" in text
    assert "work runtime releases require --no-start as an explicit lifecycle guard" in text
    assert "missing work Telegram service requires a clean first release" in text
    assert "inbound_restored=" in text
    assert "outbound=ready" in text
    assert '"${media_wrapper_path}" self-check' in text
    assert '"${current_link}/scripts/run-work-telegram-monitor-voice.sh" --self-check >/dev/null || return 1' in text
    assert text.index('"${media_wrapper_path}" self-check') < text.index(
        '"${current_link}/scripts/run-work-telegram-monitor-voice.sh" --self-check >/dev/null || return 1'
    )
    assert "-m autostop_manager.telegram_transcribe --account personal --self-check" in text
    assert "/usr/bin/timeout --signal=TERM --kill-after=10s 120s" in text
    assert "HF_HUB_OFFLINE=1" in text
    assert "rm -rf" not in text
    assert "autostop-work-telegram-media" in text
    assert 'work_runtime_root="/opt/autostop-work-telegram-runtimes"' in text
    assert 'work_model_link="/opt/autostop-work-telegram-models/faster-whisper-small"' in text
    assert 'unlink -- "${media_wrapper_path}"' in text
    assert "docker" not in text


@pytest.mark.skipif(os.geteuid() != 0, reason="deploy-script fixture requires its root-only path")
@pytest.mark.parametrize(
    ("active_state", "unit_state", "voice_exit", "inbound_intent"),
    [
        ("inactive", "disabled", 1, False),
        ("inactive", "disabled", 0, False),
        ("active", "enabled", 0, False),
        ("active", "enabled", 0, True),
        ("inactive", "enabled", 0, False),
        ("failed", "disabled", 0, False),
        ("activating", "disabled", 0, False),
        ("inactive", "masked", 0, False),
    ],
)
def test_work_deploy_restores_transport_and_rolls_back_failed_checks(
    tmp_path, active_state, unit_state, voice_exit, inbound_intent
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "origin.git"
    release_root = tmp_path / "releases"
    unit_path = tmp_path / "systemd" / "autostop-work-telegram.service"
    media_wrapper_path = tmp_path / "sbin" / "autostop-work-telegram-media"
    runtime_root = tmp_path / "runtimes"
    venv_link = tmp_path / "venv"
    model_link = tmp_path / "models" / "faster-whisper-small"
    fake_bin = tmp_path / "bin"
    daemon_reload_count = tmp_path / "daemon-reload-count"
    active_state_path = tmp_path / "active-state"
    unit_state_path = tmp_path / "unit-state"
    monitor_env = tmp_path / "monitor.env"
    source.mkdir()
    for relative_path, content in {
        "deploy/systemd/autostop-work-telegram.service": "[Service]\nExecStart=/bin/true\n",
        "scripts/run-work-telegram-media.sh": "#!/usr/bin/env bash\nexit 0\n",
        "scripts/run-work-telegram-monitor-voice.sh": f"#!/usr/bin/env bash\nexit {voice_exit}\n",
        "deploy/telegram/faster-whisper-small.sha256": "fixture-model-manifest\n",
    }.items():
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for command in (
        ["git", "init", str(source)],
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(source), "config", "user.name", "AutoStop Test"],
        ["git", "-C", str(source), "add", "."],
        ["git", "-C", str(source), "commit", "-m", "fixture"],
        ["git", "-C", str(source), "branch", "-M", "AutostopManager"],
        ["git", "init", "--bare", str(remote)],
        ["git", "-C", str(source), "remote", "add", "origin", str(remote)],
        ["git", "-C", str(source), "push", "-u", "origin", "AutostopManager"],
    ):
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr

    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    previous_runtime = runtime_root / "legacy"
    previous_venv = previous_runtime / "venv"
    previous_model = previous_runtime / "model"
    candidate_runtime = runtime_root / revision
    for runtime_dir, marker_names in (
        (previous_runtime, ()),
        (candidate_runtime, (".dependencies-ready", ".model-ready")),
    ):
        (runtime_dir / "venv" / "bin").mkdir(parents=True)
        (runtime_dir / "venv" / "bin" / "python").write_text(
            "#!/bin/sh\n"
            'case "$*" in *"telegram_bridge --account work status"*) '
            'printf \'{"ok":true,"authorized":true,"transport_ready":true,'
            '"inbound_enabled":%s}\\n\' "$FAKE_INBOUND_EXPECTED" ;; '
            "*) : ;; esac\n",
            encoding="utf-8",
        )
        (runtime_dir / "venv" / "bin" / "python").chmod(0o755)
        (runtime_dir / "model").mkdir()
        for marker_name in marker_names:
            marker = runtime_dir / marker_name
            marker.write_text("ready\n", encoding="utf-8")
            marker.chmod(0o600)
    candidate_manifest = candidate_runtime / "faster-whisper-small.sha256"
    candidate_manifest.write_text("fixture-model-manifest\n", encoding="utf-8")
    candidate_manifest.chmod(0o600)
    venv_link.symlink_to(previous_venv)
    model_link.parent.mkdir()
    model_link.symlink_to(previous_model)

    previous_release = release_root / "previous"
    (previous_release / "deploy" / "systemd").mkdir(parents=True)
    (previous_release / "scripts").mkdir()
    previous_unit = "[Service]\nExecStart=/bin/false\n"
    (previous_release / "deploy" / "systemd" / "autostop-work-telegram.service").write_text(
        previous_unit, encoding="utf-8"
    )
    (previous_release / "scripts" / "run-work-telegram-media.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    release_root.mkdir(exist_ok=True)
    (release_root / "current").symlink_to(previous_release)
    unit_path.parent.mkdir()
    unit_path.write_text(previous_unit, encoding="utf-8")
    media_wrapper_path.parent.mkdir()
    active_state_path.write_text(active_state, encoding="utf-8")
    unit_state_path.write_text(unit_state, encoding="utf-8")
    if inbound_intent:
        monitor_env.write_text("AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n", encoding="utf-8")

    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'case "$1" in\n'
        "  show)\n"
        '    case "$*" in *LoadState*) printf loaded ;; *ActiveState*) cat "$FAKE_ACTIVE_STATE_PATH" ;; *) cat "$FAKE_UNIT_STATE_PATH" ;; esac\n'
        "    exit 0\n"
        "    ;;\n"
        '  is-active) [[ "$(cat "$FAKE_ACTIVE_STATE_PATH")" == active ]]; exit $? ;;\n'
        '  stop) printf inactive > "$FAKE_ACTIVE_STATE_PATH"; exit 0 ;;\n'
        '  enable) printf enabled > "$FAKE_UNIT_STATE_PATH"; '
        '    [[ "${2:-}" != --now ]] || printf active > "$FAKE_ACTIVE_STATE_PATH"; exit 0 ;;\n'
        "  daemon-reload)\n"
        "    count=0\n"
        '    [[ -f "$FAKE_SYSTEMCTL_COUNT_FILE" ]] && count=$(cat "$FAKE_SYSTEMCTL_COUNT_FILE")\n'
        "    printf '%s' $((count + 1)) > \"$FAKE_SYSTEMCTL_COUNT_FILE\"\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "systemctl").chmod(0o755)
    (fake_bin / "sudo").write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "sudo").chmod(0o755)
    (fake_bin / "sleep").chmod(0o755)

    script_text = (ROOT / "scripts" / "deploy_telegram_bridge.sh").read_text(encoding="utf-8")
    replacements = {
        'SOURCE_DIR="/opt/AutostopManager"': f'SOURCE_DIR="{source}"',
        'release_root="/opt/autostop-work-telegram-releases"': f'release_root="{release_root}"',
        'unit_path="/etc/systemd/system/autostop-work-telegram.service"': f'unit_path="{unit_path}"',
        'venv_root="/opt/autostop-work-telegram-venv"': f'venv_root="{venv_link}"',
        'work_runtime_root="/opt/autostop-work-telegram-runtimes"': f'work_runtime_root="{runtime_root}"',
        'work_model_link="/opt/autostop-work-telegram-models/faster-whisper-small"': f'work_model_link="{model_link}"',
        'media_wrapper_path="/usr/local/sbin/autostop-work-telegram-media"': f'media_wrapper_path="{media_wrapper_path}"',
        'monitor_env="/etc/autostop-work-telegram/monitor.env"': f'monitor_env="{monitor_env}"',
        'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"': f'owner_notification_env="{tmp_path / "owner.env"}"',
        'control_lock="/run/autostop-work-telegram-control.lock"': f'control_lock="{tmp_path / "control.lock"}"',
    }
    for old, new in replacements.items():
        assert old in script_text
        script_text = script_text.replace(old, new)
    script = tmp_path / "deploy-telegram-bridge.sh"
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)

    completed = subprocess.run(
        [str(script), "--account", "work", "--no-start"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_SYSTEMCTL_COUNT_FILE": str(daemon_reload_count),
            "FAKE_ACTIVE_STATE_PATH": str(active_state_path),
            "FAKE_UNIT_STATE_PATH": str(unit_state_path),
            "FAKE_INBOUND_EXPECTED": "true" if inbound_intent else "false",
        },
    )

    coherent_lifecycle = (active_state, unit_state) in {("active", "enabled"), ("inactive", "disabled")}
    if not coherent_lifecycle:
        assert completed.returncode == 1
        assert "lifecycle must be active-enabled or legacy inactive-disabled" in completed.stderr
        assert not daemon_reload_count.exists()
    elif voice_exit == 0:
        assert completed.returncode == 0, completed.stderr
        assert "outbound=ready" in completed.stdout
        assert f"inbound_restored={str(inbound_intent).lower()}" in completed.stdout
        assert (release_root / "current").resolve() != previous_release
        assert venv_link.resolve() == candidate_runtime / "venv"
        assert model_link.resolve() == candidate_runtime / "model"
        assert daemon_reload_count.read_text(encoding="utf-8") == "1"
        assert active_state_path.read_text(encoding="utf-8") == "active"
        assert unit_state_path.read_text(encoding="utf-8") == "enabled"
        assert monitor_env.exists() is inbound_intent
        return
    else:
        assert completed.returncode == 1
        assert "previous release assets restored" in completed.stderr
        assert daemon_reload_count.read_text(encoding="utf-8") == "2"
    assert (release_root / "current").resolve() == previous_release
    assert unit_path.read_text(encoding="utf-8") == previous_unit
    assert venv_link.resolve() == previous_venv
    assert model_link.resolve() == previous_model


def test_telegram_dependency_installer_does_not_publish_a_unit_before_the_release() -> None:
    text = (ROOT / "scripts/install-telegram-bridge.sh").read_text(encoding="utf-8")

    assert "systemctl daemon-reload" not in text
    assert 'install -m 0644 "${unit_source}" "${unit_path}"' not in text


def test_work_telegram_duty_control_has_explicit_enable_and_disable_paths() -> None:
    script = ROOT / "scripts/set-work-telegram-duty.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "usage: $0 --enable|--disable" in text
    assert 'monitor_env="/etc/autostop-work-telegram/monitor.env"' in text
    assert 'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"' in text
    assert "work_telegram_owner_notification_config_invalid=true" in text
    assert 'release_dir="$(readlink -f -- "${release_link}" 2>/dev/null || true)"' in text
    assert '[[ -L "${release_link}" && "${release_dir}" == /opt/autostop-work-telegram-releases/*' in text
    assert all(forbidden not in text for forbidden in (" dialogs", " send", " search", " read"))


@pytest.mark.skipif(os.geteuid() != 0, reason="duty-control fixture requires its root-only path")
def test_work_telegram_duty_disable_clears_intent_and_stops_inflight_media(tmp_path) -> None:
    monitor_env, control_lock, fake_bin = (
        tmp_path / "etc" / "monitor.env",
        tmp_path / "run" / "control.lock",
        tmp_path / "bin",
    )
    bridge_state, unit_file_state, media_state = (
        tmp_path / "bridge-state",
        tmp_path / "unit-file-state",
        tmp_path / "media-state",
    )
    for path, content in (
        (monitor_env, "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n"),
        (bridge_state, "active\n"),
        (unit_file_state, "enabled\n"),
        (media_state, "deactivating\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    control_lock.parent.mkdir()
    venv_python = tmp_path / "venv-python"
    venv_python.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' \'{"ok":true,"transport_ready":true,"inbound_enabled":false}\'\n',
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "enable) printf 'enabled\\n' > \"$FAKE_UNIT_FILE_STATE\";;\n"
        "restart) printf 'active\\n' > \"$FAKE_BRIDGE_STATE\";;\n"
        'is-active) [ "$(cat "$FAKE_BRIDGE_STATE")" = active ];;\n'
        'stop) shift; for unit; do case "$unit" in autostop-work-telegram-media-*) printf \'inactive\\n\' > "$FAKE_MEDIA_STATE";; esac; done;;\n'
        'show) case "$*" in *LoadState*) printf \'loaded\\n\';; *ActiveState*) cat "$FAKE_BRIDGE_STATE";; *) cat "$FAKE_UNIT_FILE_STATE";; esac;;\n'
        'list-units) [ "$(cat "$FAKE_MEDIA_STATE")" != inactive ] && printf \'autostop-work-telegram-media-123.service loaded deactivating stop\\n\' || true;; *) exit 2;; esac\n',
        encoding="utf-8",
    )
    (fake_bin / "sudo").write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "systemctl").chmod(0o755)
    (fake_bin / "sudo").chmod(0o755)
    (fake_bin / "sleep").chmod(0o755)
    script_text = (ROOT / "scripts" / "set-work-telegram-duty.sh").read_text(encoding="utf-8")
    for old, new in (
        ('venv_python="/opt/autostop-work-telegram-venv/bin/python"', f'venv_python="{venv_python}"'),
        ('monitor_env="/etc/autostop-work-telegram/monitor.env"', f'monitor_env="{monitor_env}"'),
        (
            'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"',
            f'owner_notification_env="{tmp_path / "owner.env"}"',
        ),
        ('wake_config="/etc/autostop-work-telegram/wake.json"', f'wake_config="{tmp_path / "wake.json"}"'),
        ('control_lock="/run/autostop-work-telegram-control.lock"', f'control_lock="{control_lock}"'),
    ):
        script_text = script_text.replace(old, new, 1)
    script = tmp_path / "set-work-telegram-duty.sh"
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    completed = subprocess.run(
        [str(script), "--disable"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_BRIDGE_STATE": str(bridge_state),
            "FAKE_UNIT_FILE_STATE": str(unit_file_state),
            "FAKE_MEDIA_STATE": str(media_state),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["work_telegram_duty=disabled", "monitoring=off", "outbound=ready"]
    assert not monitor_env.exists()
    assert [path.read_text(encoding="utf-8") for path in (bridge_state, unit_file_state, media_state)] == [
        "active\n",
        "enabled\n",
        "inactive\n",
    ]


@pytest.mark.skipif(os.geteuid() != 0, reason="duty-control fixture requires its root-only path")
def test_work_telegram_duty_disable_allows_a_missing_unit_before_first_release(tmp_path) -> None:
    monitor_env = tmp_path / "etc" / "monitor.env"
    control_lock = tmp_path / "run" / "control.lock"
    fake_bin = tmp_path / "bin"
    monitor_env.parent.mkdir(parents=True)
    control_lock.parent.mkdir()
    monitor_env.write_text("AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n", encoding="utf-8")
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        '#!/bin/sh\ncase "$1" in\nshow) printf "not-found\\n";;\nlist-units) :;;\n*) exit 9;;\nesac\n',
        encoding="utf-8",
    )
    (fake_bin / "systemctl").chmod(0o755)
    script_text = (ROOT / "scripts" / "set-work-telegram-duty.sh").read_text(encoding="utf-8")
    for old, new in (
        ('monitor_env="/etc/autostop-work-telegram/monitor.env"', f'monitor_env="{monitor_env}"'),
        (
            'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"',
            f'owner_notification_env="{tmp_path / "owner.env"}"',
        ),
        ('wake_config="/etc/autostop-work-telegram/wake.json"', f'wake_config="{tmp_path / "wake.json"}"'),
        ('control_lock="/run/autostop-work-telegram-control.lock"', f'control_lock="{control_lock}"'),
    ):
        script_text = script_text.replace(old, new, 1)
    script = tmp_path / "set-work-telegram-duty.sh"
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)

    completed = subprocess.run(
        [str(script), "--disable"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["work_telegram_duty=disabled", "monitoring=off", "outbound=ready"]
    assert not monitor_env.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="duty-control fixture requires its root-only path")
def test_work_telegram_duty_rejects_unsafe_owner_config_without_touching_services(tmp_path) -> None:
    owner_config = tmp_path / "owner.env"
    owner_target = tmp_path / "owner-target.env"
    owner_target.write_text("AUTOSTOP_WORK_TELEGRAM_OWNER_PEER_ID=123456\n", encoding="utf-8")
    owner_config.symlink_to(owner_target)
    control_lock = tmp_path / "run" / "control.lock"
    control_lock.parent.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    (fake_bin / "systemctl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_LOG"\nexit 99\n', encoding="utf-8"
    )
    (fake_bin / "systemctl").chmod(0o755)
    source = (ROOT / "scripts" / "set-work-telegram-duty.sh").read_text(encoding="utf-8")
    for old, new in (
        ('monitor_env="/etc/autostop-work-telegram/monitor.env"', f'monitor_env="{tmp_path / "monitor.env"}"'),
        (
            'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"',
            f'owner_notification_env="{owner_config}"',
        ),
        ('control_lock="/run/autostop-work-telegram-control.lock"', f'control_lock="{control_lock}"'),
    ):
        source = source.replace(old, new, 1)
    script = tmp_path / "set-work-telegram-duty.sh"
    script.write_text(source, encoding="utf-8")
    script.chmod(0o755)

    completed = subprocess.run(
        [str(script), "--disable"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_SYSTEMCTL_LOG": str(systemctl_log)},
    )

    assert completed.returncode == 1
    assert "work_telegram_owner_notification_config_invalid=true" in completed.stderr
    assert not systemctl_log.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="duty-control fixture requires its root-only path")
@pytest.mark.parametrize("initial_active", [True, False])
@pytest.mark.parametrize("ready_after", [0, 2, 99])
def test_work_telegram_duty_waits_for_readiness_or_disables_on_failure(tmp_path, initial_active, ready_after) -> None:
    release_root = tmp_path / "releases"
    release = release_root / "revision"
    current_link = release_root / "current"
    monitor_env = tmp_path / "etc" / "monitor.env"
    control_lock = tmp_path / "run" / "control.lock"
    venv_python = tmp_path / "venv-python"
    fake_bin = tmp_path / "bin"
    systemctl_log = tmp_path / "systemctl.log"
    bridge_state = tmp_path / "bridge-state"
    unit_state = tmp_path / "unit-state"
    probe_count = tmp_path / "probe-count"
    bridge_state.write_text("active" if initial_active else "inactive", encoding="utf-8")
    unit_state.write_text("disabled", encoding="utf-8")
    release.mkdir(parents=True)
    current_link.symlink_to(release)
    monitor_env.parent.mkdir()
    control_lock.parent.mkdir()
    venv_python.write_text(
        '#!/bin/sh\nif [ ! -e "$FAKE_MONITOR_ENV" ]; then '
        'printf \'%s\\n\' \'{"ok":true,"transport_ready":true,"inbound_enabled":false}\'; exit 0; fi\n'
        'count=0\n[ ! -f "$FAKE_PROBE_COUNT" ] || count=$(cat "$FAKE_PROBE_COUNT")\n'
        'count=$((count + 1))\nprintf "%s" "$count" > "$FAKE_PROBE_COUNT"\n'
        '[ "$count" -gt "$FAKE_READY_AFTER" ] || exit 1\n'
        'printf \'%s\\n\' \'{"ok": true, "monitor": {"enabled": true, "retention": "memory_only"}}\'\n',
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        'is-active) [ "$(cat "$FAKE_BRIDGE_STATE")" = active ];;\n'
        'enable|restart) printf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_LOG"; printf active > "$FAKE_BRIDGE_STATE"; printf enabled > "$FAKE_UNIT_STATE";;\n'
        'disable) printf disabled > "$FAKE_UNIT_STATE";;\n'
        'stop) printf inactive > "$FAKE_BRIDGE_STATE";;\n'
        "list-units) :;;\n"
        'show) case "$*" in *LoadState*) printf loaded;; *ActiveState*) cat "$FAKE_BRIDGE_STATE";; *) cat "$FAKE_UNIT_STATE";; esac;;\n'
        "*) exit 2;;\nesac\n",
        encoding="utf-8",
    )
    (fake_bin / "sudo").write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in fake_bin.iterdir():
        command.chmod(0o755)
    script_text = (ROOT / "scripts" / "set-work-telegram-duty.sh").read_text(encoding="utf-8")
    for old, new in (
        ('release_link="/opt/autostop-work-telegram-releases/current"', f'release_link="{current_link}"'),
        ('venv_python="/opt/autostop-work-telegram-venv/bin/python"', f'venv_python="{venv_python}"'),
        ('monitor_env="/etc/autostop-work-telegram/monitor.env"', f'monitor_env="{monitor_env}"'),
        (
            'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"',
            f'owner_notification_env="{tmp_path / "owner.env"}"',
        ),
        ('wake_config="/etc/autostop-work-telegram/wake.json"', f'wake_config="{tmp_path / "wake.json"}"'),
        ('control_lock="/run/autostop-work-telegram-control.lock"', f'control_lock="{control_lock}"'),
        ("/opt/autostop-work-telegram-releases/*", f"{release_root}/*"),
    ):
        script_text = script_text.replace(old, new, 1)
    script = tmp_path / "set-work-telegram-duty.sh"
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)

    completed = subprocess.run(
        [str(script), "--enable"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
            "FAKE_BRIDGE_STATE": str(bridge_state),
            "FAKE_UNIT_STATE": str(unit_state),
            "FAKE_PROBE_COUNT": str(probe_count),
            "FAKE_READY_AFTER": str(ready_after),
            "FAKE_MONITOR_ENV": str(monitor_env),
        },
    )

    if ready_after == 99:
        assert completed.returncode == 1
        assert "work_telegram_duty_enable_failed=true" in completed.stderr
        assert not monitor_env.exists()
        assert bridge_state.read_text(encoding="utf-8") == "active"
        assert unit_state.read_text(encoding="utf-8") == "enabled"
        assert int(probe_count.read_text(encoding="utf-8")) == 15
        return

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "work_telegram_duty=enabled",
        "monitoring=enabled",
        "retention=memory_only",
    ]
    assert monitor_env.read_text(encoding="utf-8") == "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n"
    assert int(probe_count.read_text(encoding="utf-8")) == ready_after + 1
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == (
        ["enable autostop-work-telegram.service", "restart autostop-work-telegram.service"]
        if initial_active
        else ["enable --now autostop-work-telegram.service"]
    )


def test_telegram_admin_scripts_require_an_explicit_account_selector() -> None:
    for script_name in (
        "deploy_telegram_bridge.sh",
        "install-telegram-bridge.sh",
    ):
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / script_name)],
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 2
        assert "--account personal|work" in completed.stderr


def test_work_model_provisioner_is_syntax_valid_and_scoped() -> None:
    script = ROOT / "scripts/provision-telegram-transcription-model.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "usage: $0 --account work --revision <commit>" in text
    assert 'work_runtime_root="/opt/autostop-work-telegram-runtimes"' in text
    assert 'model_link="${model_link_root}/faster-whisper-small"' in text
    assert 'target_model="${work_runtime_dir}/model"' in text
    assert 'mktemp -d "${work_runtime_dir}/.model.XXXXXX"' in text
    assert "configure_work_candidate_runtime" in text
    assert "prepare_legacy_model_link" in text
    assert "model_ready_marker" in text
    assert "--no-dereference" in text
    assert "sha256sum -c --status" in text
    assert "source_transcription_model_invalid=true" in text
    assert "autostop-work-telegram" in text
    assert "telegram_release_source_invalid=true" in text
    assert "transcription_model_manifest_revision_mismatch=true" in text
    assert "work_telegram_runtime_lifecycle_unsafe_for_model_update=true" in text
    assert "autostop-work-telegram-control.lock" in text
    assert "PYTHONPATH" not in text
    assert "curl" not in text
    assert "wget" not in text


def test_work_media_sandbox_wrapper_is_scoped_and_has_no_bridge_access() -> None:
    script = ROOT / "scripts/run-work-telegram-media.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "transcribe|preview" in text
    assert "self-check" in text
    assert "--account" in text and "work" in text
    assert "systemd-run --quiet --wait --pipe --collect" in text
    assert "PrivateNetwork=true" in text
    assert "InaccessiblePaths=-/var/lib/autostop-work-telegram" in text
    assert "InaccessiblePaths=/var/lib/autostop-work-telegram" in text
    assert "/etc/autostop-work-telegram" in text
    assert "/run/autostop-work-telegram/bridge.sock" in text
    assert "ReadWritePaths=${inbox_dir}" in text
    assert "HF_HUB_OFFLINE=1" in text
    assert "command+=(--self-check)" in text
    assert 'runtime_properties=(\n  --property="RuntimeMaxSec=180s"' in text
    assert '--property="TimeoutStopSec=10s"' in text
    assert '--property="PartOf=autostop-work-telegram.service"' in text
    assert '"${runtime_properties[@]}"' in text
    assert '--property="ReadWritePaths=${inbox_dir}"' in text
    preview_runtime_start = text.index('if [[ "${action}" == "preview" ]]')
    preview_runtime_end = text.index("\nfi", preview_runtime_start)
    preview_runtime = text[preview_runtime_start:preview_runtime_end]
    assert "MemoryDenyWriteExecute=true" in preview_runtime
    assert "MemoryDenyWriteExecute=true" not in text[text.index("systemd-run") :]
    media_branch = text[
        text.index('if [[ "${self_check}" -eq 1 ]]', text.index("path_properties=(")) : text.index("systemd-run")
    ]
    self_check_branch, file_action_branch = media_branch.split("\nelse\n", 1)
    assert "ReadWritePaths" not in self_check_branch
    assert "InaccessiblePaths=-/var/lib" in self_check_branch
    assert "ReadWritePaths" in file_action_branch
    assert "InaccessiblePaths=/var/lib" in file_action_branch
    assert "InaccessiblePaths=-/var/lib" not in file_action_branch
    assert '"${path_properties[@]}"' in text
    assert "telegram_bridge" not in text


def test_telegram_skill_requires_verified_private_audio_cleanup() -> None:
    text = (ROOT / ".agents/skills/manage-owner-telegram/SKILL.md").read_text(encoding="utf-8")

    assert "--delete-after" in text
    assert "после ошибки" in text


def test_model_manifest_is_static_and_has_only_model_payloads() -> None:
    manifest = ROOT / "deploy/telegram/faster-whisper-small.sha256"

    entries = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]

    assert [entry.rsplit("  ", 1)[1] for entry in entries] == [
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    ]
    assert all(len(entry.split("  ", 1)[0]) == 64 for entry in entries)


def test_telegram_dependency_lock_is_hash_pinned_and_installer_requires_it() -> None:
    lock = ROOT / "deploy/telegram/requirements-py312-linux-x86_64.lock"
    build_lock = ROOT / "deploy/telegram/build-requirements-py312-linux-x86_64.lock"
    source_lock = ROOT / "deploy/telegram/pyaes-source-py312-linux-x86_64.lock"
    wheel_lock = ROOT / "deploy/telegram/pyaes-wheel-py312-linux-x86_64.lock"
    installer = (ROOT / "scripts/install-telegram-bridge.sh").read_text(encoding="utf-8")

    entries = [line for line in lock.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    build_entries = [
        line for line in build_lock.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
    ]
    source_entries = [
        line for line in source_lock.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
    ]
    wheel_entries = [
        line for line in wheel_lock.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
    ]

    assert len(entries) >= 20
    assert all("==" in entry and "--hash=sha256:" in entry for entry in entries)
    assert len(build_entries) == 2
    assert all("==" in entry and "--hash=sha256:" in entry for entry in build_entries)
    assert len(source_entries) == len(wheel_entries) == 1
    assert source_entries[0] != wheel_entries[0]
    assert "--require-hashes --no-deps" in installer
    assert "--no-build-isolation" in installer
    assert "--only-binary=:all:" in installer
    assert "--no-index" in installer
    assert "pyaes_wheel_sha256" in installer
    assert "pip check" in installer
    assert "requirements-py312-linux-x86_64.lock" in installer
    assert 'faster-whisper==1.2.1"' not in installer


def test_authorization_script_supports_both_accounts_without_message_operations() -> None:
    script = ROOT / "scripts/authorize-telegram-account.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "usage: $0 --account personal|work" in text
    assert '--account "${account}" code-login' in text
    assert '--account "${account}" probe' in text
    assert "umask 077" in text
    assert "umask 077; exec" in text
    assert "verify_private_session_files" in text
    assert "clear_work_monitor_intent" not in text
    assert 'monitor_env="/etc/autostop-work-telegram/monitor.env"' in text
    assert 'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"' in text
    assert "work_telegram_monitor_config_invalid=true" in text
    assert "work_telegram_owner_notification_config_invalid=true" in text
    assert "restore_original_service_state" in text
    assert 'systemctl disable "${service_unit}"' in text
    assert '"${service_user}:${service_user}:600"' in text
    assert "autostop-telegram.service" in text
    assert "autostop-work-telegram.service" in text
    assert all(forbidden not in text for forbidden in (" dialogs", " send", " search", " read"))


def _run_mocked_authorization(
    tmp_path: Path,
    *,
    scenario: str,
    session_mode: str = "600",
    account: str = "work",
    monitor_intent: bool = False,
    monitor_intent_directory: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, Path | None, Path]:
    release_link = tmp_path / "release"
    venv_python = tmp_path / "venv" / "bin" / "python"
    session_file = tmp_path / "state" / "account.session"
    release_link.mkdir()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    venv_python.chmod(0o755)
    session_file.parent.mkdir()
    session_file.write_text("mock-session", encoding="utf-8")
    session_file.chmod(0o600)

    service_user = "autostop-telegram" if account == "personal" else "autostop-work-telegram"
    release_root = "autostop-telegram-releases" if account == "personal" else "autostop-work-telegram-releases"
    venv_root = "autostop-telegram-venv" if account == "personal" else "autostop-work-telegram-venv"
    state_root = "autostop-telegram" if account == "personal" else "autostop-work-telegram"

    source = (ROOT / "scripts/authorize-telegram-account.sh").read_text(encoding="utf-8")
    source = source.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then", 1)
    source = source.replace(f'service_user="{service_user}"', 'service_user="mock-service"', 1)
    source = source.replace(
        f'release_link="/opt/{release_root}/current"',
        f'release_link="{release_link}"',
        1,
    )
    source = source.replace(
        f'venv_python="/opt/{venv_root}/bin/python"',
        f'venv_python="{venv_python}"',
        1,
    )
    source = source.replace(
        f'session_base="/var/lib/{state_root}/account.session"',
        f'session_base="{session_file}"',
        1,
    )
    monitor_env: Path | None = None
    if account == "work":
        monitor_env = tmp_path / "monitor.env"
        source = source.replace(
            'monitor_env="/etc/autostop-work-telegram/monitor.env"',
            f'monitor_env="{monitor_env}"',
            1,
        )
        source = source.replace(
            'owner_notification_env="/etc/autostop-work-telegram/owner-notification.env"',
            f'owner_notification_env="{tmp_path / "owner.env"}"',
            1,
        )
        source = source.replace(
            'control_lock="/run/autostop-work-telegram-control.lock"',
            f'control_lock="{tmp_path / "control.lock"}"',
            1,
        )
        if monitor_intent:
            monitor_env.write_text(
                "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n",
                encoding="utf-8",
            )
        if monitor_intent_directory:
            monitor_env.mkdir()
    authorize_script = tmp_path / "authorize.sh"
    authorize_script.write_text(source, encoding="utf-8")
    authorize_script.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
command_name="$1"
shift
printf '%s %s\n' "$command_name" "$*" >>"$FAKE_SYSTEMCTL_LOG"
case "$command_name" in
  show) printf 'loaded\n' ;;
  is-active) [[ "$(<"$FAKE_ACTIVE")" == 1 ]] ;;
  is-enabled) [[ "$(<"$FAKE_ENABLED")" == 1 ]] ;;
  stop) printf '0\n' >"$FAKE_ACTIVE" ;;
  start) printf '1\n' >"$FAKE_ACTIVE" ;;
  enable)
    printf '1\n' >"$FAKE_ENABLED"
    if [[ "${1:-}" == "--now" ]]; then
      printf '1\n' >"$FAKE_ACTIVE"
    fi
    ;;
  disable) printf '0\n' >"$FAKE_ENABLED" ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *" probe" ]]; then
  probe_count=0
  if [[ -f "$FAKE_PROBE_COUNT" ]]; then
    probe_count="$(<"$FAKE_PROBE_COUNT")"
  fi
  probe_count="$((probe_count + 1))"
  printf '%s\n' "$probe_count" >"$FAKE_PROBE_COUNT"
  if [[ "$FAKE_SCENARIO" == active_authorized || ( "$FAKE_SCENARIO" == existing && "$probe_count" -ge 2 ) ]]; then
    printf '{"ok": true, "authorized": true}\n'
    exit 0
  fi
  printf '{"ok": true, "authorized": false}\n'
  exit 1
fi
if [[ "$*" == *" code-login" ]]; then
  if [[ "$FAKE_SCENARIO" == existing ]]; then
    printf '{"ok": false, "error": "account_already_authorized"}\n'
  else
    printf '{"ok": false, "error": "code_login_failed"}\n'
  fi
  exit 1
fi
exit 2
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)

    stat = fake_bin / "stat"
    stat.write_text(
        "#!/bin/sh\n"
        'case "$*" in *monitor.env|*owner.env) exec /usr/bin/stat "$@" ;; esac\n'
        "printf 'mock-service:mock-service:%s\\n' \"$FAKE_SESSION_MODE\"\n",
        encoding="utf-8",
    )
    stat.chmod(0o755)

    active = tmp_path / "active"
    enabled = tmp_path / "enabled"
    probe_count = tmp_path / "probe-count"
    systemctl_log = tmp_path / "systemctl.log"
    active.write_text("1\n", encoding="utf-8")
    enabled.write_text("1\n", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_ACTIVE": str(active),
            "FAKE_ENABLED": str(enabled),
            "FAKE_PROBE_COUNT": str(probe_count),
            "FAKE_SCENARIO": scenario,
            "FAKE_SESSION_MODE": session_mode,
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        }
    )
    completed = subprocess.run(
        ["bash", str(authorize_script), "--account", account],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    state = f"active={active.read_text().strip()},enabled={enabled.read_text().strip()}"
    return completed, state, monitor_env, systemctl_log


@pytest.mark.parametrize("account", ["personal", "work"])
def test_authorization_failure_restores_selected_service_state(tmp_path, account: str) -> None:
    completed, state, _monitor_env, _systemctl_log = _run_mocked_authorization(
        tmp_path,
        scenario="failure",
        account=account,
    )

    assert completed.returncode == 1
    assert f"{account}_telegram_login_failed=true" in completed.stderr
    assert state == "active=1,enabled=1"


def test_work_authorization_recovers_an_existing_private_session(tmp_path) -> None:
    completed, state, _monitor_env, _systemctl_log = _run_mocked_authorization(tmp_path, scenario="existing")

    assert completed.returncode == 0
    assert "work_telegram_already_authorized=true" in completed.stdout
    assert state == "active=1,enabled=1"


def test_work_authorization_reexecs_an_active_authorized_bridge_and_preserves_monitor_intent(tmp_path) -> None:
    completed, state, monitor_env, systemctl_log = _run_mocked_authorization(
        tmp_path,
        scenario="active_authorized",
        monitor_intent=True,
    )

    assert completed.returncode == 0
    assert "work_telegram_already_authorized=true" in completed.stdout
    assert state == "active=1,enabled=1"
    assert monitor_env is not None
    assert monitor_env.read_text(encoding="utf-8") == "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n"
    commands = systemctl_log.read_text(encoding="utf-8")
    assert "stop autostop-work-telegram.service" in commands
    assert "enable --now autostop-work-telegram.service" in commands
    assert commands.index("stop autostop-work-telegram.service") < commands.index(
        "enable --now autostop-work-telegram.service"
    )


def test_work_authorization_rejects_invalid_monitor_intent_without_stopping_transport(tmp_path) -> None:
    completed, state, monitor_env, systemctl_log = _run_mocked_authorization(
        tmp_path,
        scenario="active_authorized",
        monitor_intent_directory=True,
    )

    assert completed.returncode == 1
    assert "work_telegram_monitor_config_invalid=true" in completed.stderr
    assert state == "active=1,enabled=1"
    assert monitor_env is not None
    assert monitor_env.is_dir()
    commands = systemctl_log.read_text(encoding="utf-8")
    assert "stop autostop-work-telegram.service" not in commands
    assert "disable autostop-work-telegram.service" not in commands


def test_work_authorization_rejects_an_existing_open_session_before_login(tmp_path) -> None:
    completed, state, _monitor_env, _systemctl_log = _run_mocked_authorization(
        tmp_path,
        scenario="failure",
        session_mode="644",
    )

    assert completed.returncode == 1
    assert "work_session_permissions_invalid=true" in completed.stderr
    assert state == "active=0,enabled=0"


def test_telegram_install_script_supports_an_isolated_work_account() -> None:
    script = ROOT / "scripts/install-telegram-bridge.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "--account personal|work" in text
    assert "autostop-work-telegram" in text
    assert "/etc/autostop-work-telegram" in text
    assert "work_config_directory_permissions_invalid" in text
    assert "cp --no-dereference" in text
    assert "/opt/autostop-work-telegram-venv" in text
    assert 'work_runtime_root="/opt/autostop-work-telegram-runtimes"' in text
    assert "prepare_work_legacy_venv" in text
    assert "prepare_work_candidate_runtime" in text
    assert "finalize_work_candidate_runtime" in text
    assert ".dependencies-ready" in text
    assert "source_credentials_permissions_invalid" in text
    assert "--require-hashes --no-deps" in text
    assert "requirements-py312-linux-x86_64.lock" in text
    assert "usage: $0 --account personal|work --revision <commit>" in text
    assert "telegram_release_source_invalid=true" in text
    assert "work_telegram_runtime_lifecycle_unsafe_for_dependency_update=true" in text
    assert "autostop-work-telegram-control.lock" in text


def test_private_download_reader_handles_short_system_reads(monkeypatch, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    content = b"%PDF-1.7\ncomplete-content"
    download = inbox / "42-example.pdf"
    download.write_bytes(content)
    download.chmod(0o600)
    real_read = telegram_bridge.os.read

    monkeypatch.setattr(
        telegram_bridge.os,
        "read",
        lambda descriptor, count: real_read(descriptor, min(count, 3)),
    )

    assert _read_private_download(download, inbox_dir=inbox) == content


def test_credentials_require_private_permissions_and_valid_values(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)

    config = TelegramConfig.load(credentials)

    assert config.api_id == 12345678
    assert config.api_hash.endswith("cdef")


def test_credentials_fail_closed_when_group_readable(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o640)

    with pytest.raises(BridgeError, match="credentials_permissions_too_open"):
        TelegramConfig.load(credentials)


def test_credentials_reject_symlink_without_reading_target(tmp_path) -> None:
    target = tmp_path / "real-credentials"
    target.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    target.chmod(0o600)
    credentials = tmp_path / "credentials"
    credentials.symlink_to(target.name)

    with pytest.raises(BridgeError, match="credentials_unavailable"):
        TelegramConfig.load(credentials)


def test_contract_key_is_exact_private_regular_file(tmp_path) -> None:
    key_path = tmp_path / "state" / "contract.key"

    key = _ensure_private_key(key_path)

    assert len(key) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert _ensure_private_key(key_path) == key


def test_runtime_paths_support_an_isolated_second_account(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)
    state_dir = tmp_path / "assistant-state"
    session = state_dir / "account"
    socket_path = tmp_path / "assistant-run" / "bridge.sock"

    config = TelegramConfig.load(
        credentials,
        session_path=session,
        state_dir=state_dir,
        socket_path=socket_path,
    )

    assert config.session_path == session
    assert config.state_dir == state_dir
    assert config.socket_path == socket_path


def test_runtime_paths_reject_relative_or_cross_account_session(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)

    with pytest.raises(BridgeError, match="runtime_path_not_absolute"):
        TelegramConfig.load(credentials, session_path=Path("relative/account"))
    with pytest.raises(BridgeError, match="session_state_mismatch"):
        TelegramConfig.load(
            credentials,
            session_path=tmp_path / "account-a" / "account",
            state_dir=tmp_path / "account-b",
        )


def test_parser_requires_an_explicit_named_account() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["probe"])

    args = build_parser().parse_args(["--account", "work", "probe"])
    assert args.account == "work"
    assert args.command == "probe"


def test_named_work_account_owns_all_runtime_paths() -> None:
    args = build_parser().parse_args(["--account", "work", "daemon"])
    paths = telegram_bridge.ACCOUNT_PATHS[args.account]

    assert paths.credentials_path == telegram_bridge.WORK_CREDENTIALS_PATH
    assert paths.session_path == telegram_bridge.WORK_SESSION_PATH
    assert paths.state_dir == telegram_bridge.WORK_STATE_DIR
    assert paths.socket_path == telegram_bridge.WORK_SOCKET_PATH


def test_work_owner_peer_is_loaded_only_from_the_runtime_environment(monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    credentials.chmod(0o600)
    state_dir = tmp_path / "state"
    paths = telegram_bridge.TelegramAccountPaths(
        credentials_path=credentials,
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=tmp_path / "run" / "bridge.sock",
    )
    monkeypatch.setattr(telegram_bridge, "_account_paths", lambda _account: paths)
    monkeypatch.setenv(telegram_bridge.WORK_OWNER_PEER_ENVIRONMENT, "123456789")

    work = telegram_bridge._config_for_account("work")
    personal = telegram_bridge._config_for_account("personal")

    assert work.owner_peer_id == 123456789
    assert personal.owner_peer_id is None
    monkeypatch.setenv(telegram_bridge.WORK_OWNER_PEER_ENVIRONMENT, "@owner")
    with pytest.raises(BridgeError, match="owner_peer_id_invalid"):
        telegram_bridge._config_for_account("work")


def test_named_accounts_have_fixed_isolated_media_paths() -> None:
    personal_inbox = account_inbox_dir("personal")
    work_inbox = account_inbox_dir("work")

    assert account_outbox_dir("personal") == telegram_bridge.DEFAULT_OUTBOX_DIR
    assert account_model_dir("personal") == telegram_bridge.DEFAULT_STATE_DIR / "models" / "faster-whisper-small"
    assert work_inbox == telegram_bridge.WORK_SOCKET_PATH.parent / "inbox"
    assert account_outbox_dir("work") == telegram_bridge.WORK_SOCKET_PATH.parent / "outbox"
    assert account_model_dir("work") == telegram_bridge.WORK_TRANSCRIPTION_MODEL_DIR
    assert work_inbox != personal_inbox
    with pytest.raises(BridgeError, match="account_invalid"):
        account_inbox_dir("unknown")


def test_named_account_rejects_manual_runtime_path_overrides(tmp_path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--account", "work", "--socket", str(tmp_path / "bridge.sock"), "probe"])


def test_phone_normalization_is_strict_and_handles_russian_trunk_prefix() -> None:
    assert normalize_phone("+7 (999) 111-22-33") == "+79991112233"
    assert normalize_phone("8 999 111 22 33") == "+79991112233"
    with pytest.raises(BridgeError, match="phone_invalid"):
        normalize_phone("client@example.com")


def test_send_contract_binds_target_text_and_expiry() -> None:
    secret = b"x" * 32
    token = issue_send_contract(secret, peer_id=10, text="hello", last_message_id=7, now=100)

    payload = verify_send_contract(token, secret, peer_id=10, text="hello", now=101)

    assert payload["last_message_id"] == 7
    with pytest.raises(BridgeError, match="contract_target_changed"):
        verify_send_contract(token, secret, peer_id=11, text="hello", now=101)
    with pytest.raises(BridgeError, match="contract_text_changed"):
        verify_send_contract(token, secret, peer_id=10, text="changed", now=101)
    with pytest.raises(BridgeError, match="contract_expired"):
        verify_send_contract(token, secret, peer_id=10, text="hello", now=100 + 901)


def test_send_contract_binds_reply_target_and_source() -> None:
    secret = b"x" * 32
    digest = "a" * 64
    token = issue_send_contract(
        secret,
        peer_id=10,
        text="hello",
        last_message_id=7,
        reply_to_message_id=5,
        reply_message_sha256=digest,
        now=100,
    )

    payload = verify_send_contract(
        token,
        secret,
        peer_id=10,
        text="hello",
        reply_to_message_id=5,
        reply_message_sha256=digest,
        now=101,
    )

    assert payload["reply_to_message_id"] == 5
    with pytest.raises(BridgeError, match="contract_reply_target_changed"):
        verify_send_contract(
            token,
            secret,
            peer_id=10,
            text="hello",
            reply_to_message_id=6,
            reply_message_sha256=digest,
            now=101,
        )
    with pytest.raises(BridgeError, match="contract_reply_source_changed"):
        verify_send_contract(
            token,
            secret,
            peer_id=10,
            text="hello",
            reply_to_message_id=5,
            reply_message_sha256="b" * 64,
            now=101,
        )


def test_photo_contract_binds_target_caption_photo_and_expiry() -> None:
    secret = b"x" * 32
    digest = "a" * 64
    token = issue_photo_contract(
        secret,
        peer_id=10,
        caption="hello",
        photo_sha256=digest,
        last_message_id=7,
        now=100,
    )

    payload = verify_photo_contract(
        token,
        secret,
        peer_id=10,
        caption="hello",
        photo_sha256=digest,
        now=101,
    )

    assert payload["last_message_id"] == 7
    with pytest.raises(BridgeError, match="contract_photo_changed"):
        verify_photo_contract(
            token,
            secret,
            peer_id=10,
            caption="hello",
            photo_sha256="b" * 64,
            now=101,
        )


def test_download_contract_binds_target_message_media_and_expiry() -> None:
    secret = b"x" * 32
    token = issue_download_contract(
        secret,
        peer_id=10,
        message_id=22,
        media_fingerprint="a" * 64,
        now=100,
    )

    payload = verify_download_contract(
        token,
        secret,
        peer_id=10,
        message_id=22,
        media_fingerprint="a" * 64,
        now=101,
    )

    assert payload["message_id"] == 22
    with pytest.raises(BridgeError, match="contract_message_changed"):
        verify_download_contract(
            token,
            secret,
            peer_id=10,
            message_id=23,
            media_fingerprint="a" * 64,
            now=101,
        )
    with pytest.raises(BridgeError, match="contract_media_changed"):
        verify_download_contract(
            token,
            secret,
            peer_id=10,
            message_id=22,
            media_fingerprint="b" * 64,
            now=101,
        )


def test_apply_requires_idempotency_key() -> None:
    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_send_request({"peer": "@target", "text": "hello", "mode": "apply"})

    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_photo_request({"peer": "@target", "photo": "/run/example.jpg", "caption": "hello", "mode": "apply"})
    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_download_request({"peer": "@target", "message_id": 10, "mode": "apply"})


def test_owner_notification_contract_has_no_caller_supplied_target() -> None:
    request = {
        "operation": "send_owner_notification",
        "text": "digest",
        "mode": "dry_run",
    }

    assert telegram_bridge.validate_owner_notification_request(request, owner_peer_id=10) == (
        "10",
        "digest",
        "dry_run",
        "",
        0,
    )
    with pytest.raises(BridgeError, match="owner_peer_not_configured"):
        telegram_bridge.validate_owner_notification_request(request, owner_peer_id=None)
    for override in ({"peer": "11"}, {"address": "@owner"}, {"reply_to_message_id": 5}):
        with pytest.raises(BridgeError, match="owner_notification_request_invalid"):
            telegram_bridge.validate_owner_notification_request(request | override, owner_peer_id=10)


def test_only_external_apply_operations_take_the_mutation_lock() -> None:
    assert _requires_mutation_lock({"operation": "send", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "send_owner_notification", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "send_photo", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "send", "mode": "dry_run"}) is False
    assert _requires_mutation_lock({"operation": "download", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "discard_download"}) is True
    assert _requires_mutation_lock({"operation": "monitor_mark"}) is True
    assert _requires_mutation_lock({"operation": "monitor_voice_stage"}) is False
    assert _requires_mutation_lock({"operation": "monitor_voice_discard"}) is False
    assert telegram_bridge._requires_monitor_voice_lock({"operation": "monitor_voice_stage"}) is False
    assert telegram_bridge._requires_monitor_voice_lock({"operation": "monitor_voice_discard"}) is True
    assert _requires_mutation_lock({"operation": "read"}) is False


def test_text_send_rejects_non_private_peer(monkeypatch, tmp_path) -> None:
    async def resolve_peer(_client, _peer):
        return object(), {"id": 10, "title": "Group", "username": None, "kind": "group"}

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve_peer)

    with pytest.raises(BridgeError, match="private_peer_required"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                _runtime_config(tmp_path),
                {"operation": "send", "peer": "10", "text": "hello", "mode": "dry_run"},
            )
        )


def test_photo_send_rejects_non_private_peer(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    outbox = config.socket_path.parent / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    photo = outbox / "frame.jpg"
    photo.write_bytes(b"\xff\xd8example\xff\xd9")
    photo.chmod(0o600)

    async def resolve_peer(_client, _peer):
        return object(), {"id": 10, "title": "Channel", "username": None, "kind": "channel"}

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve_peer)

    with pytest.raises(BridgeError, match="private_peer_required"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                config,
                {"operation": "send_photo", "peer": "10", "photo": str(photo), "caption": "hello", "mode": "dry_run"},
            )
        )


def test_photo_file_must_be_private_jpeg_inside_outbox(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    outbox.chmod(0o700)
    photo = outbox / "frame.jpg"
    photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(photo, 0o600)

    resolved, digest, size = validate_photo_file(photo, outbox_dir=outbox)

    assert resolved == photo
    assert len(digest) == 64
    assert size == len(b"\xff\xd8example\xff\xd9")


def test_photo_file_rejects_open_permissions_and_wrong_directory(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    outbox.chmod(0o700)
    open_photo = outbox / "open.jpg"
    open_photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(open_photo, 0o640)
    outside_photo = tmp_path / "outside.jpg"
    outside_photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(outside_photo, 0o600)

    with pytest.raises(BridgeError, match="photo_permissions_invalid"):
        validate_photo_file(open_photo, outbox_dir=outbox)
    with pytest.raises(BridgeError, match="photo_path_invalid"):
        validate_photo_file(outside_photo, outbox_dir=outbox)


def test_photo_file_rejects_symlink_without_reading_target(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir(mode=0o700)
    target = outbox / "target.jpg"
    target.write_bytes(b"\xff\xd8example\xff\xd9")
    target.chmod(0o600)
    photo = outbox / "frame.jpg"
    photo.symlink_to(target.name)

    with pytest.raises(BridgeError, match="photo_unavailable"):
        validate_photo_file(photo, outbox_dir=outbox)

    assert target.read_bytes() == b"\xff\xd8example\xff\xd9"


def test_send_photo_uploads_the_bytes_that_were_validated(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "run"
    outbox = runtime_dir / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    photo = outbox / "frame.jpg"
    original = b"\xff\xd8validated-frame\xff\xd9"
    photo.write_bytes(original)
    photo.chmod(0o600)
    state_dir = tmp_path / "state"
    config = TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=runtime_dir / "bridge.sock",
    )
    entity = object()

    async def resolve_peer(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def last_message_id(_client, _entity):
        return 30 if _client.uploaded else 20

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve_peer)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message_id)

    class Client:
        uploaded = b""
        send_count = 0

        async def send_file(self, _entity, upload, **_kwargs):
            photo.write_bytes(b"\xff\xd8swapped-frame\xff\xd9")
            self.uploaded = upload.read()
            self.send_count += 1
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            return SimpleNamespace(message="caption", media=object())

    client = Client()
    dry_run = asyncio.run(
        telegram_bridge._handle_send_photo(
            client,
            config,
            {"peer": "10", "photo": str(photo), "caption": "caption", "mode": "dry_run"},
        )
    )
    result = asyncio.run(
        telegram_bridge._handle_send_photo(
            client,
            config,
            {
                "peer": "10",
                "photo": str(photo),
                "caption": "caption",
                "mode": "apply",
                "contract_token": dry_run["contract_token"],
                "idempotency_key": "photo-test-key",
            },
        )
    )

    assert result["verified"] is True
    assert client.uploaded == original

    photo.write_bytes(original)
    replayed = asyncio.run(
        telegram_bridge._handle_send_photo(
            client,
            config,
            {
                "peer": "10",
                "photo": str(photo),
                "caption": "caption",
                "mode": "apply",
                "contract_token": dry_run["contract_token"],
                "idempotency_key": "photo-test-key",
            },
        )
    )
    assert replayed["replayed"] is True and replayed["verified"] is True
    assert client.send_count == 1


def test_send_request_rejects_empty_or_oversized_text() -> None:
    with pytest.raises(BridgeError, match="message_length_invalid"):
        validate_send_request({"peer": "@target", "text": "", "mode": "dry_run"})
    with pytest.raises(BridgeError, match="message_length_invalid"):
        validate_send_request({"peer": "@target", "text": "x" * 4097, "mode": "dry_run"})
    with pytest.raises(BridgeError, match="reply_to_message_id_invalid"):
        validate_send_request({"peer": "@target", "text": "hello", "mode": "dry_run", "reply_to_message_id": -1})
    with pytest.raises(BridgeError, match="reply_to_message_id_invalid"):
        validate_send_request({"peer": "@target", "text": "hello", "mode": "dry_run", "reply_to_message_id": True})


def test_sensitive_telegram_and_vpn_uris_are_redacted() -> None:
    text, redacted = redact_sensitive_message_text("profiles vpn://secret-value and tg://login?token=secret-token")

    assert text == "profiles [redacted_sensitive_uri] and [redacted_sensitive_uri]"
    assert redacted is True


def test_normal_message_text_is_unchanged() -> None:
    text, redacted = redact_sensitive_message_text("Привет! Как дела?")

    assert text == "Привет! Как дела?"
    assert redacted is False


def _inbound_monitor(**kwargs):
    monitor = telegram_bridge.InboundMonitor(**kwargs)
    monitor._next_sequence = 1  # Stable refs for lifecycle fixtures; epoch isolation is tested separately.
    return monitor


def test_inbound_monitor_keeps_only_bounded_private_message_references() -> None:
    monitor = _inbound_monitor(max_events=2)

    class EventWithoutInputPeer:
        is_private = True
        out = False
        chat_id = 20
        id = 99
        message = "private text must never enter monitor state"
        title = "private title must never enter monitor state"

        @property
        def input_chat(self):
            pytest.fail("monitor must not resolve or retain Telegram input peers")

    monitor.record(EventWithoutInputPeer())
    monitor.record(
        SimpleNamespace(
            is_private=True,
            out=False,
            chat_id=20,
            id=100,
            message="private text must never enter monitor state",
            title="private title must never enter monitor state",
        )
    )
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    monitor.record(SimpleNamespace(is_private=True, out=True, chat_id=20, id=101))
    monitor.record(SimpleNamespace(is_private=False, out=False, chat_id=20, id=101))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=101))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=102))

    status = monitor.status()
    events = monitor.events()

    assert status["pending_events"] == 2
    assert status["retained_events"] == 2
    assert status["open_events"] == 2
    assert status["reply_verified_events"] == 0
    assert status["no_reply_needed_events"] == 0
    assert status["dropped_events"] == 2
    assert status["dropped_open_events"] == 2
    assert status["retention"] == "memory_only"
    assert [event["event_id"] for event in events] == ["inbound-3", "inbound-4"]
    assert all(event["state"] == "open" for event in events)
    assert "private text" not in json.dumps({"status": status, "events": events})
    assert "private title" not in json.dumps({"status": status, "events": events})
    assert monitor.resolve("inbound-3").message_id == 101
    assert not hasattr(monitor.resolve("inbound-3"), "input_peer")
    with pytest.raises(BridgeError, match="inbound_event_invalid"):
        monitor.resolve("20")
    with pytest.raises(BridgeError, match="inbound_event_unavailable"):
        monitor.resolve("inbound-2")


def test_inbound_monitor_restart_does_not_rebind_old_event_to_another_client() -> None:
    old, current = telegram_bridge.InboundMonitor(), telegram_bridge.InboundMonitor()
    for monitor, peer in ((old, 10), (current, 20)):
        monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=peer, id=1))
    old_ref = old.events()[0]["event_id"]
    current_ref = current.events()[0]["event_id"]
    assert current.resolve(current_ref).peer_id == 20
    with pytest.raises(BridgeError, match="inbound_event_unavailable"):
        current.mark_no_reply_needed(old_ref)
    assert current.status()["open_events"] == 1


def test_inbound_monitor_evicts_closed_refs_before_open_refs() -> None:
    monitor = _inbound_monitor(max_events=2)
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=101))

    assert monitor.mark_no_reply_needed("inbound-1") == {"event_id": "inbound-1", "state": "no_reply_needed"}

    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=102))

    status = monitor.status()
    events = monitor.events()
    assert [event["event_id"] for event in events] == ["inbound-2", "inbound-3"]
    assert all(event["state"] == "open" for event in events)
    assert status["pending_events"] == 2
    assert status["dropped_events"] == 1
    assert status["dropped_open_events"] == 0
    assert "peer_id" not in json.dumps({"status": status, "events": events})


def test_inbound_monitor_callback_discards_bad_provider_events() -> None:
    monitor = _inbound_monitor()

    class BadEvent:
        @property
        def is_private(self):
            raise RuntimeError("provider data unavailable")

    asyncio.run(telegram_bridge._capture_incoming_event(monitor, BadEvent()))

    assert monitor.status()["pending_events"] == 0


def test_inbound_monitor_context_excludes_stale_same_peer_refs() -> None:
    monitor = _inbound_monitor()
    anchor_time = datetime(2026, 9, 11, 12, tzinfo=UTC)
    monitor._events = deque(
        [
            telegram_bridge.InboundMonitorEvent(
                sequence=1,
                peer_id=20,
                message_id=100,
                received_at=(anchor_time.replace(hour=11, minute=54)).isoformat(),
            ),
            telegram_bridge.InboundMonitorEvent(
                sequence=2,
                peer_id=20,
                message_id=101,
                received_at=anchor_time.isoformat(),
            ),
            telegram_bridge.InboundMonitorEvent(
                sequence=3,
                peer_id=20,
                message_id=102,
                received_at=anchor_time.isoformat(),
            ),
        ]
    )

    context = monitor.context("inbound-2", limit=5)

    assert [event.sequence for event in context] == [2, 3]


def test_monitor_operations_are_read_only_and_fetch_an_explicit_event(monkeypatch, tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    config = _runtime_config(tmp_path)
    message = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="test vpn://sensitive",
        media=None,
    )

    class Client:
        async def get_messages(self, actual_entity, *, ids):
            assert actual_entity is None
            assert ids == 100
            return message

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("monitor read must not send")

        async def send_read_acknowledge(self, *_args, **_kwargs):
            pytest.fail("monitor read must not acknowledge")

        async def download_media(self, *_args, **_kwargs):
            pytest.fail("monitor read must not download")

    events = asyncio.run(
        telegram_bridge._handle_operation(Client(), config, {"operation": "monitor_events"}, inbound_monitor=monitor)
    )
    read = asyncio.run(
        telegram_bridge._handle_operation(
            Client(),
            config,
            {"operation": "monitor_read", "event_id": "inbound-1"},
            inbound_monitor=monitor,
        )
    )

    assert events["events"] and "peer_id" not in json.dumps(events)
    assert "test vpn" not in json.dumps(events)
    assert read["event_id"] == "inbound-1"
    assert read["message"]["text"] == "test [redacted_sensitive_uri]"
    assert read["message"]["has_media"] is False
    assert telegram_bridge._requires_mutation_lock({"operation": "monitor_events"}) is False
    with pytest.raises(BridgeError, match="inbound_monitor_disabled"):
        asyncio.run(telegram_bridge._handle_operation(Client(), config, {"operation": "monitor_status"}))
    message.chat_id = 21
    with pytest.raises(BridgeError, match="inbound_message_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "monitor_read", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )


def test_monitor_mark_requires_explicit_no_reply_disposition_and_keeps_payload_opaque(tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))

    marked = asyncio.run(
        telegram_bridge._handle_operation(
            object(),
            _runtime_config(tmp_path),
            {"operation": "monitor_mark", "event_id": "inbound-1", "disposition": "no_reply_needed"},
            inbound_monitor=monitor,
        )
    )

    assert marked == {"ok": True, "event": {"event_id": "inbound-1", "state": "no_reply_needed"}}
    assert monitor.status()["pending_events"] == 0
    assert monitor.status()["no_reply_needed_events"] == 1
    assert monitor.mark_no_reply_needed("inbound-1") == {"event_id": "inbound-1", "state": "no_reply_needed"}
    assert monitor.mark_reply_verified(peer_id=20, message_id=100) is False
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=101))
    assert monitor.mark_reply_verified(peer_id=20, message_id=101) is True
    with pytest.raises(BridgeError, match="inbound_event_already_closed"):
        monitor.mark_no_reply_needed("inbound-2")
    assert "peer_id" not in json.dumps(marked)
    with pytest.raises(BridgeError, match="inbound_disposition_invalid"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                _runtime_config(tmp_path),
                {"operation": "monitor_mark", "event_id": "inbound-1", "disposition": "reply_verified"},
                inbound_monitor=monitor,
            )
        )


def test_monitor_read_uses_bounded_exact_dialog_fallback_after_global_failure(tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    message = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="hello",
        media=None,
    )

    class Client:
        def __init__(self) -> None:
            self.global_ids: list[int] = []
            self.fallback_ids: list[int] = []
            self.dialog_limits: list[int] = []
            self.exact_entity = object()

        async def get_messages(self, actual_entity, *, ids):
            if actual_entity is None:
                self.global_ids.append(ids)
                raise RuntimeError("global message lookup unavailable")
            assert actual_entity is self.exact_entity
            self.fallback_ids.append(ids)
            return message

        async def iter_dialogs(self, *, limit):
            self.dialog_limits.append(limit)
            yield SimpleNamespace(id=30, entity=object())
            yield SimpleNamespace(id=20, entity=self.exact_entity)

        async def get_entity(self, *_args, **_kwargs):
            pytest.fail("monitor fallback must not resolve a Telegram entity")

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("monitor fallback must not send")

        async def send_read_acknowledge(self, *_args, **_kwargs):
            pytest.fail("monitor fallback must not acknowledge")

        async def download_media(self, *_args, **_kwargs):
            pytest.fail("monitor fallback must not download")

    client = Client()
    read = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            _runtime_config(tmp_path),
            {"operation": "monitor_read", "event_id": "inbound-1"},
            inbound_monitor=monitor,
        )
    )

    assert read["message"]["text"] == "hello"
    assert client.global_ids == [100]
    assert client.fallback_ids == [100]
    assert client.dialog_limits == [20]
    assert "peer_id" not in json.dumps(read)


def test_monitor_fallback_fails_closed_without_exact_dialog_or_message_binding(tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))

    class MissingDialogClient:
        async def get_messages(self, actual_entity, *, ids):
            if actual_entity is None:
                raise RuntimeError("global message lookup unavailable")
            pytest.fail("fallback must not read a non-matching dialog")

        async def iter_dialogs(self, *, limit):
            assert limit == 20
            yield SimpleNamespace(id=21, entity=object())

    with pytest.raises(BridgeError, match="inbound_message_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                MissingDialogClient(),
                _runtime_config(tmp_path),
                {"operation": "monitor_read", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )

    mismatched_message = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=21,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="wrong chat",
        media=None,
    )

    class MismatchedMessageClient:
        exact_entity = object()

        async def get_messages(self, actual_entity, *, ids):
            if actual_entity is None:
                raise RuntimeError("global message lookup unavailable")
            assert actual_entity is self.exact_entity
            assert ids == 100
            return mismatched_message

        async def iter_dialogs(self, *, limit):
            assert limit == 20
            yield SimpleNamespace(id=20, entity=self.exact_entity)

    with pytest.raises(BridgeError, match="inbound_message_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                MismatchedMessageClient(),
                _runtime_config(tmp_path),
                {"operation": "monitor_read", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )


def test_monitor_context_reads_photo_and_voice_refs_from_one_private_chat_only(tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=30, id=200))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=101))

    voice_attribute = type("DocumentAttributeAudio", (), {})()
    voice_attribute.voice = True
    voice_attribute.duration = 8
    photo = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="photo caption",
        media=SimpleNamespace(),
        photo=SimpleNamespace(),
    )
    voice = SimpleNamespace(
        id=101,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="voice request",
        media=SimpleNamespace(),
        photo=None,
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=32),
        document=SimpleNamespace(attributes=[voice_attribute]),
    )

    class Client:
        def __init__(self) -> None:
            self.global_ids: list[list[int]] = []
            self.fallback_ids: list[list[int]] = []
            self.dialog_limits: list[int] = []
            self.exact_entity = object()

        async def get_messages(self, actual_entity, *, ids):
            if actual_entity is None:
                self.global_ids.append(ids)
                raise RuntimeError("global message lookup unavailable")
            assert actual_entity is self.exact_entity
            self.fallback_ids.append(ids)
            return [{100: photo, 101: voice}[message_id] for message_id in ids]

        async def iter_dialogs(self, *, limit):
            self.dialog_limits.append(limit)
            yield SimpleNamespace(id=30, entity=object())
            yield SimpleNamespace(id=20, entity=self.exact_entity)

        async def get_entity(self, *_args, **_kwargs):
            pytest.fail("monitor context must not resolve a Telegram entity")

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("monitor context must not send")

        async def send_read_acknowledge(self, *_args, **_kwargs):
            pytest.fail("monitor context must not acknowledge")

        async def download_media(self, *_args, **_kwargs):
            pytest.fail("monitor context must not download")

    client = Client()
    context = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            _runtime_config(tmp_path),
            {"operation": "monitor_context", "event_id": "inbound-1", "limit": 5},
            inbound_monitor=monitor,
        )
    )

    assert client.global_ids == [[100, 101]]
    assert client.fallback_ids == [[100, 101]]
    assert client.dialog_limits == [20]
    assert [row["event_id"] for row in context["messages"]] == ["inbound-1", "inbound-3"]
    assert [row["message"]["media_kind"] for row in context["messages"]] == ["photo", "voice"]
    assert "peer_id" not in json.dumps(context)
    assert telegram_bridge._requires_mutation_lock({"operation": "monitor_context"}) is False
    with pytest.raises(BridgeError, match="inbound_context_limit_invalid"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                _runtime_config(tmp_path),
                {"operation": "monitor_context", "event_id": "inbound-1", "limit": 6},
                inbound_monitor=monitor,
            )
        )

    class MissingVoiceClient(Client):
        async def get_messages(self, actual_entity, *, ids):
            if actual_entity is None:
                raise RuntimeError("global message lookup unavailable")
            assert actual_entity is self.exact_entity
            return [photo, None]

    with pytest.raises(BridgeError, match="inbound_message_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                MissingVoiceClient(),
                _runtime_config(tmp_path),
                {"operation": "monitor_context", "event_id": "inbound-1", "limit": 5},
                inbound_monitor=monitor,
            )
        )


def test_monitor_voice_stages_one_exact_voice_without_exposing_source_refs(monkeypatch, tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    config = _runtime_config(tmp_path)
    monkeypatch.setattr(telegram_bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: None)
    audio = b"OggSprivate-voice"
    voice_attribute = type("DocumentAttributeAudio", (), {})()
    voice_attribute.voice = True
    voice_attribute.duration = 8
    voice = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="voice body must not enter the monitor response",
        media=SimpleNamespace(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[voice_attribute]),
    )

    class Client:
        def __init__(self):
            self.downloads = 0

        async def get_messages(self, actual_entity, *, ids):
            assert actual_entity is None
            assert ids == 100
            return voice

        async def download_media(self, message, *, file):
            assert message is voice
            assert file is bytes
            self.downloads += 1
            return audio

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("monitor voice must not send")

        async def send_read_acknowledge(self, *_args, **_kwargs):
            pytest.fail("monitor voice must not acknowledge")

        async def iter_dialogs(self, **_kwargs):
            pytest.fail("exact monitor lookup must not enumerate dialogs")
            yield None

    client = Client()
    staged = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "monitor_voice_stage",
                "event_id": "inbound-1",
            },
            inbound_monitor=monitor,
        )
    )
    staged_json = json.dumps(staged)
    assert staged["event_id"] == "inbound-1"
    assert "media_handle" in staged
    assert all(marker not in staged_json for marker in ("peer_id", "message_id", "saved_path", "voice body"))
    path = config.socket_path.parent / "inbox" / f"monitor-{staged['media_handle']}.ogg"
    assert path.read_bytes() == audio
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (config.state_dir / "idempotency.json").exists()
    assert monitor.events()[0]["state"] == "open"

    with pytest.raises(BridgeError, match="inbound_voice_stage_active"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                {"operation": "monitor_voice_stage", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )
    assert client.downloads == 1

    discarded = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "monitor_voice_discard", "media_handle": staged["media_handle"]},
            inbound_monitor=monitor,
        )
    )
    assert discarded == {"ok": True, "cleanup_verified": True}
    assert not path.exists()
    assert telegram_bridge._requires_mutation_lock({"operation": "monitor_voice_stage"}) is False
    assert telegram_bridge._requires_monitor_voice_lock({"operation": "monitor_voice_stage"}) is False

    missing_handle = "m" * 32
    monitor.reserve_voice_stage(monitor.require_open("inbound-1"))
    monitor.stage_voice(
        monitor.require_open("inbound-1"),
        missing_handle,
        config.socket_path.parent / "inbox" / f"monitor-{missing_handle}.ogg",
        duration_seconds=8,
        suffix=".ogg",
    )
    with pytest.raises(BridgeError, match="inbound_voice_cleanup_unverified"):
        asyncio.run(telegram_bridge._handle_monitor_voice_discard(config, monitor, handle=missing_handle))
    assert monitor.voice_stage(missing_handle) is not None
    assert asyncio.run(
        telegram_bridge._handle_monitor_voice_discard(
            config, monitor, handle=missing_handle, runner_cleanup_verified=True
        )
    ) == {"ok": True, "cleanup_verified": True}


def test_monitor_voice_stage_cleans_up_when_event_closes_while_download_is_awaited(tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    config = _runtime_config(tmp_path)
    audio = b"OggSprivate-voice"
    voice_attribute = type("DocumentAttributeAudio", (), {})()
    voice_attribute.voice = True
    voice_attribute.duration = 8
    voice = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="",
        media=SimpleNamespace(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[voice_attribute]),
    )

    async def exercise() -> None:
        started = asyncio.Event()
        continue_download = asyncio.Event()

        class Client:
            async def get_messages(self, actual_entity, *, ids):
                assert actual_entity is None
                assert ids == 100
                return voice

            async def download_media(self, message, *, file):
                assert message is voice
                assert file is bytes
                started.set()
                await continue_download.wait()
                return audio

        task = asyncio.create_task(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "monitor_voice_stage", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )
        await started.wait()
        assert monitor.mark_no_reply_needed("inbound-1")["state"] == "no_reply_needed"
        continue_download.set()
        with pytest.raises(BridgeError, match="inbound_event_not_open"):
            await task

    asyncio.run(exercise())
    assert not list((config.socket_path.parent / "inbox").glob("monitor-*"))


def test_monitor_voice_stage_timeout_releases_its_reservation(monkeypatch, tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    config = _runtime_config(tmp_path)
    audio = b"OggSprivate-voice"
    voice_attribute = type("DocumentAttributeAudio", (), {})()
    voice_attribute.voice = True
    voice_attribute.duration = 8
    voice = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=20,
        date=datetime(2026, 9, 11, tzinfo=UTC),
        message="",
        media=SimpleNamespace(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[voice_attribute]),
    )

    class Client:
        async def get_messages(self, actual_entity, *, ids):
            assert actual_entity is None
            assert ids == 100
            return voice

        async def download_media(self, _message, *, file):
            assert file is bytes
            await asyncio.Event().wait()

    monkeypatch.setattr(telegram_bridge, "MONITOR_VOICE_STAGE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(BridgeError, match="inbound_voice_download_timeout"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "monitor_voice_stage", "event_id": "inbound-1"},
                inbound_monitor=monitor,
            )
        )
    event = monitor.require_open("inbound-1")
    monitor.reserve_voice_stage(event)
    monitor.release_voice_stage_reservation(event)


def test_monitor_voice_stage_expiry_removes_an_abandoned_file(monkeypatch, tmp_path) -> None:
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=20, id=100))
    config = _runtime_config(tmp_path)
    event = monitor.require_open("inbound-1")
    handle = "m" * 32
    monitor.reserve_voice_stage(event)
    path = telegram_bridge._save_monitor_voice_stage(
        b"OggSabandoned-voice",
        handle=handle,
        suffix=".ogg",
        inbox_dir=config.socket_path.parent / "inbox",
    )
    monitor.stage_voice(event, handle, path, duration_seconds=8, suffix=".ogg")
    monkeypatch.setattr(telegram_bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    monkeypatch.setattr(telegram_bridge, "MONITOR_VOICE_STAGE_CLEANUP_RETRIES", 1)

    asyncio.run(telegram_bridge._expire_monitor_voice_stage(config, monitor, handle))

    assert not path.exists()
    assert monitor.voice_stage(handle) is None


def _runtime_config(tmp_path, *, owner_peer_id: int | None = None) -> TelegramConfig:
    runtime_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    return TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=runtime_dir / "bridge.sock",
        owner_peer_id=owner_peer_id,
    )


def test_target_classification_covers_private_bot_group_and_channels() -> None:
    class Utils:
        @staticmethod
        def get_peer_id(entity):
            return entity.id

    User = type("User", (), {})
    Chat = type("Chat", (), {})
    private = User()
    private.id, private.first_name, private.last_name, private.bot = 1, "A", "B", False
    bot = User()
    bot.id, bot.username, bot.bot = 2, "helper", True
    group = Chat()
    group.id, group.title = -3, "Group"
    channel = SimpleNamespace(id=-4, title="Channel", broadcast=True)
    supergroup = SimpleNamespace(id=-5, title="Supergroup", megagroup=True)

    assert telegram_bridge._target_from_entity(private, Utils)["kind"] == "private"
    assert telegram_bridge._target_from_entity(bot, Utils)["kind"] == "bot"
    assert telegram_bridge._target_from_entity(group, Utils)["kind"] == "group"
    assert telegram_bridge._target_from_entity(channel, Utils)["kind"] == "channel"
    assert telegram_bridge._target_from_entity(supergroup, Utils)["kind"] == "supergroup"


def test_read_only_bridge_operations_are_bounded_and_redacted(monkeypatch, tmp_path) -> None:
    User = type("User", (), {})
    me = User()
    me.id, me.first_name, me.last_name, me.username, me.bot = 10, "Owner", "Account", "owner", False
    entity = User()
    entity.id, entity.first_name, entity.last_name, entity.username, entity.bot = 20, "Exact", "Target", None, False

    class Utils:
        @staticmethod
        def get_peer_id(value):
            return value.id

    class Client:
        async def get_me(self):
            return me

        async def iter_dialogs(self, *, limit):
            assert limit == 2
            yield SimpleNamespace(entity=entity, unread_count=3)

        async def get_messages(self, _entity, *, limit):
            assert limit == 2
            return [
                SimpleNamespace(
                    id=2,
                    date=datetime(2026, 8, 16, tzinfo=UTC),
                    out=True,
                    message="vpn://secret",
                    media=SimpleNamespace(),
                ),
                SimpleNamespace(id=1, date=None, out=False, message="hello", media=None),
            ]

    async def resolve(_client, _peer):
        return entity, telegram_bridge._target_from_entity(entity, Utils)

    async def search(_client, query, limit):
        assert query == "Exact"
        assert limit == 3
        return [(entity, telegram_bridge._target_from_entity(entity, Utils))]

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (object(), Utils, object()))
    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_search_entities", search)
    config = _runtime_config(tmp_path)
    client = Client()

    probe = asyncio.run(telegram_bridge._handle_operation(client, config, {"operation": "probe"}))
    status = asyncio.run(telegram_bridge._handle_operation(client, config, {"operation": "status"}))
    dialogs = asyncio.run(telegram_bridge._handle_operation(client, config, {"operation": "dialogs", "limit": 2}))
    search_result = asyncio.run(
        telegram_bridge._handle_operation(client, config, {"operation": "search", "query": "Exact", "limit": 3})
    )
    read = asyncio.run(
        telegram_bridge._handle_operation(client, config, {"operation": "read", "peer": "20", "limit": 2})
    )

    assert probe == {"ok": True, "authorized": True}
    assert status["authorized"] is True
    assert status["transport_ready"] is True
    assert status["inbound_enabled"] is False
    assert status["owner_notification_configured"] is False
    monitored_status = asyncio.run(
        telegram_bridge._handle_operation(client, config, {"operation": "status"}, inbound_monitor=_inbound_monitor())
    )
    assert monitored_status["transport_ready"] is True
    assert monitored_status["inbound_enabled"] is True
    assert dialogs["dialogs"][0]["unread_count"] == 3
    assert search_result["matches"][0]["id"] == 20
    assert read["messages"][1]["text"] == "[redacted_sensitive_uri]"
    assert read["messages"][1]["sensitive_content_redacted"] is True


def test_resolve_phone_returns_only_a_verified_private_target(monkeypatch, tmp_path) -> None:
    User = type("User", (), {})
    entity = User()
    entity.id, entity.first_name, entity.last_name = 20, "Exact", "Target"
    entity.username, entity.bot, entity.contact = "private_name", False, False

    class Client:
        async def __call__(self, request):
            assert request.phone == "+79991112233"
            return SimpleNamespace(peer=SimpleNamespace(user_id=20), users=[entity])

    class Utils:
        @staticmethod
        def get_peer_id(value):
            return value.id

    functions = SimpleNamespace(
        contacts=SimpleNamespace(ResolvePhoneRequest=lambda *, phone: SimpleNamespace(phone=phone))
    )
    monkeypatch.setattr(telegram_bridge, "_PHONE_RESOLVE_LAST_AT", 0.0)
    monkeypatch.setattr(telegram_bridge, "_load_telegram_functions", lambda: (functions, Utils))
    result = asyncio.run(
        telegram_bridge._handle_operation(
            Client(),
            _runtime_config(tmp_path),
            {"operation": "resolve_phone", "phone": "+7 (999) 111-22-33"},
        )
    )

    assert result == {
        "ok": True,
        "resolved": True,
        "target": {"id": 20, "kind": "private", "is_contact": False},
    }
    assert "phone" not in json.dumps(result)
    assert "private_name" not in json.dumps(result)


def test_resolve_phone_fails_closed_and_rate_limits(monkeypatch, tmp_path) -> None:
    class PhoneNotOccupiedError(Exception):
        pass

    class Client:
        async def __call__(self, _request):
            raise PhoneNotOccupiedError

    functions = SimpleNamespace(
        contacts=SimpleNamespace(ResolvePhoneRequest=lambda *, phone: SimpleNamespace(phone=phone))
    )
    monkeypatch.setattr(telegram_bridge, "_PHONE_RESOLVE_LAST_AT", 0.0)
    monkeypatch.setattr(telegram_bridge, "_load_telegram_functions", lambda: (functions, object()))
    monkeypatch.setattr(telegram_bridge.time, "monotonic", lambda: 10.0)
    request = {"operation": "resolve_phone", "phone": "+79991112233"}
    with pytest.raises(BridgeError, match="phone_not_resolved"):
        asyncio.run(telegram_bridge._handle_operation(Client(), _runtime_config(tmp_path), request))
    with pytest.raises(BridgeError, match="phone_resolve_rate_limited"):
        asyncio.run(telegram_bridge._handle_operation(Client(), _runtime_config(tmp_path), request))


def test_download_media_requires_dry_run_and_saves_private_verified_file(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"%PDF-1.7\nexample"
    message = SimpleNamespace(
        id=44,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="inspection.pdf", mime_type="application/pdf", size=len(content)),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 44
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 44, "mode": "dry_run"},
        )
    )

    assert dry["media"]["downloadable"] is True
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 44,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "download-test-key",
            },
        )
    )

    saved_path = Path(applied["saved_path"])
    assert saved_path.read_bytes() == content
    assert saved_path.stat().st_mode & 0o777 == 0o600
    discarded = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "discard_download", "path": str(saved_path)},
        )
    )
    assert discarded["removed"] is True
    assert not saved_path.exists()


def test_telegram_voice_is_downloadable_when_duration_is_bounded(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"OggS" + b"voice-data"
    AudioAttribute = type("DocumentAttributeAudio", (), {})
    audio_attribute = AudioAttribute()
    audio_attribute.duration = 42
    audio_attribute.voice = True
    message = SimpleNamespace(
        id=46,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="audio.ogg", mime_type="audio/ogg", size=len(content)),
        document=SimpleNamespace(attributes=[audio_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 46
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 46, "mode": "dry_run"},
        )
    )

    assert dry["media"]["downloadable"] is True
    assert dry["media"]["voice"] is True
    assert dry["media"]["duration_seconds"] == 42
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 46,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "voice-download-test-key",
            },
        )
    )
    assert Path(applied["saved_path"]).suffix == ".ogg"


def test_telegram_voice_over_ten_minutes_is_not_downloadable(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    AudioAttribute = type("DocumentAttributeAudio", (), {})
    audio_attribute = AudioAttribute()
    audio_attribute.duration = 601
    audio_attribute.voice = True
    message = SimpleNamespace(
        id=47,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="audio.ogg", mime_type="audio/ogg", size=100),
        document=SimpleNamespace(attributes=[audio_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 47
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 47, "mode": "dry_run"},
            )
        )


def test_telegram_short_mp4_video_is_downloadable(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"\x00\x00\x00\x18ftypisom" + b"video-data"
    VideoAttribute = type("DocumentAttributeVideo", (), {})
    video_attribute = VideoAttribute()
    video_attribute.duration = 45
    video_attribute.w = 1920
    video_attribute.h = 1080
    message = SimpleNamespace(
        id=48,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="clip.mp4", mime_type="video/mp4", size=len(content)),
        document=SimpleNamespace(attributes=[video_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 48
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 48, "mode": "dry_run"},
        )
    )

    assert dry["media"] == {
        "downloadable": True,
        "duration_seconds": 45,
        "file_name": "clip.mp4",
        "height": 1080,
        "media_type": "SimpleNamespace",
        "mime_type": "video/mp4",
        "size_bytes": len(content),
        "suffix": ".mp4",
        "video": True,
        "voice": False,
        "width": 1920,
    }
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 48,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "video-download-test-key",
            },
        )
    )
    assert applied["verified"] is True
    assert Path(applied["saved_path"]).suffix == ".mp4"


@pytest.mark.parametrize("duration", [0, 121])
def test_telegram_mp4_video_requires_bounded_duration(monkeypatch, tmp_path, duration) -> None:
    config = _runtime_config(tmp_path)
    VideoAttribute = type("DocumentAttributeVideo", (), {})
    video_attribute = VideoAttribute()
    video_attribute.duration = duration
    video_attribute.w = 1280
    video_attribute.h = 720
    message = SimpleNamespace(
        id=49,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="clip.mp4", mime_type="video/mp4", size=100),
        document=SimpleNamespace(attributes=[video_attribute]),
    )

    async def resolve(_client, _peer):
        return object(), {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 49
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 49, "mode": "dry_run"},
            )
        )


def test_video_mp4_download_rejects_invalid_container_signature() -> None:
    with pytest.raises(BridgeError, match="download_content_invalid"):
        telegram_bridge._validate_download_content(
            b"not-an-mp4-container",
            mime_type="video/mp4",
            suffix=".mp4",
        )


def test_download_rejects_unsupported_or_oversized_media(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    message = SimpleNamespace(
        id=45,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="archive.zip", mime_type="application/zip", size=100),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 45
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 45, "mode": "dry_run"},
            )
        )


def test_discard_download_rejects_symlink(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    inbox = config.socket_path.parent / "inbox"
    inbox.mkdir(parents=True, mode=0o700)
    target = inbox / "target.pdf"
    target.write_bytes(b"%PDF-1.7\nexample")
    target.chmod(0o600)
    link = inbox / "link.pdf"
    link.symlink_to(target.name)

    with pytest.raises(BridgeError, match="download_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                config,
                {"operation": "discard_download", "path": str(link)},
            )
        )

    assert target.exists()


def test_text_send_dry_run_apply_replay_and_conflict(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    target = {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def resolve(_client, _peer):
        return entity, target

    async def last_message(_client, _entity):
        return 30 if _client.sent else 20

    class Client:
        def __init__(self):
            self.sent: list[str] = []

        async def send_message(self, _entity, text):
            self.sent.append(text)
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            return SimpleNamespace(message=self.sent[-1])

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "hello", "mode": "dry_run"},
        )
    )
    apply_request = {
        "operation": "send",
        "peer": "10",
        "text": "hello",
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": "text-key",
    }
    applied = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))
    replayed = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))

    assert applied["verified"] is True
    assert replayed["replayed"] is True
    assert replayed["verified"] is True
    assert client.sent == ["hello"]

    changed_dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "changed", "mode": "dry_run"},
        )
    )
    with pytest.raises(BridgeError, match="idempotency_key_conflict"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                apply_request | {"text": "changed", "contract_token": changed_dry["contract_token"]},
            )
        )


def test_owner_notification_dry_run_apply_replay_and_exact_readback(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path, owner_peer_id=10)
    entity = object()
    target = {"id": 10, "title": "Private owner", "username": "not-returned", "kind": "private"}

    async def resolve(_client, peer):
        assert peer in {"10", "11"}
        return entity, target | {"id": int(peer)}

    async def last_message(_client, _entity):
        return 30 if _client.sent else 20

    class Client:
        def __init__(self):
            self.sent: list[str] = []

        async def send_message(self, _entity, text):
            self.sent.append(text)
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            return SimpleNamespace(id=30, message=self.sent[-1], out=True)

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry_request = {
        "operation": "send_owner_notification",
        "text": "CRM: одно изменение",
        "mode": "dry_run",
    }
    dry = asyncio.run(telegram_bridge._handle_operation(client, config, dry_request))
    apply_request = dry_request | {
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": "crm-digest:window-1",
    }

    applied = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))
    replayed = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))
    readback = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "owner_notification_readback",
                "message_id": applied["message_id"],
                "expected_text_sha256": dry["text_sha256"],
            },
        )
    )

    assert dry["target"] == {"kind": "private", "role": "owner"}
    assert applied["target"] == {"kind": "private", "role": "owner"}
    assert applied["verified"] is True
    assert replayed["replayed"] is True
    assert readback == {
        "ok": True,
        "target": {"kind": "private", "role": "owner"},
        "message_id": 30,
        "text_sha256": dry["text_sha256"],
        "verified": True,
    }
    assert client.sent == ["CRM: одно изменение"]
    saved_idempotency = json.loads((config.state_dir / "idempotency.json").read_text(encoding="utf-8"))
    assert saved_idempotency["crm-digest:window-1"]["operation"] == "send_owner_notification"
    assert saved_idempotency["crm-digest:window-1"]["target_role"] == "owner"
    assert "peer_id" not in saved_idempotency["crm-digest:window-1"]
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    contract_payload = telegram_bridge._decode_contract(dry["contract_token"], contract_key)
    assert contract_payload["peer_id"] != config.owner_peer_id
    with pytest.raises(BridgeError, match="contract_target_changed"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                _runtime_config(tmp_path, owner_peer_id=11),
                apply_request | {"idempotency_key": "crm-digest:other-window"},
            )
        )

    with pytest.raises(BridgeError, match="owner_notification_readback_mismatch"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                {
                    "operation": "owner_notification_readback",
                    "message_id": 30,
                    "expected_text_sha256": "0" * 64,
                },
            )
        )


def test_text_reply_binds_source_sends_and_verifies_reply(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    target = {"id": 10, "title": "Target", "username": None, "kind": "private"}
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=10, id=5))

    async def resolve(_client, _peer):
        return entity, target

    async def last_message(_client, _entity):
        return 20

    class Client:
        def __init__(self):
            self.source_text = "original question"
            self.sent: list[tuple[str, int]] = []

        async def send_message(self, _entity, text, *, reply_to):
            self.sent.append((text, reply_to))
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            if ids == 5:
                return SimpleNamespace(
                    id=5,
                    message=self.source_text,
                    out=False,
                    date=datetime(2026, 8, 25, tzinfo=UTC),
                )
            assert ids == 30
            return SimpleNamespace(message=self.sent[-1][0], reply_to_msg_id=self.sent[-1][1])

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "send",
                "peer": "10",
                "text": "answer",
                "mode": "dry_run",
                "reply_to_message_id": 5,
            },
        )
    )
    assert dry["reply_source"]["message_id"] == 5
    assert dry["reply_source"]["out"] is False

    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "send",
                "peer": "10",
                "text": "answer",
                "mode": "apply",
                "reply_to_message_id": 5,
                "contract_token": dry["contract_token"],
                "idempotency_key": "reply-key",
            },
            inbound_monitor=monitor,
        )
    )

    assert client.sent == [("answer", 5)]
    assert applied["verified"] is True
    assert applied["reply_verified"] is True
    assert applied["reply_to_message_id"] == 5
    assert applied["monitor_event_closed"] is True
    assert monitor.status()["pending_events"] == 0
    assert monitor.status()["reply_verified_events"] == 1
    assert monitor.events()[0]["state"] == "reply_verified"


def test_unverified_reply_does_not_close_inbound_monitor_event(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    monitor = _inbound_monitor()
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=10, id=5))

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def last_message(_client, _entity):
        return 30 if _client.sent_count else 20

    class Client:
        sent_count = 0
        reply_text = "wrong reply"

        async def send_message(self, _entity, _text, *, reply_to):
            assert reply_to == 5
            self.sent_count += 1
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            if ids == 5:
                return SimpleNamespace(id=5, message="question", out=False, date=None)
            assert ids == 30
            return SimpleNamespace(message=self.reply_text, reply_to_msg_id=5)

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "answer", "mode": "dry_run", "reply_to_message_id": 5},
            inbound_monitor=monitor,
        )
    )

    apply_request = {
        "operation": "send",
        "peer": "10",
        "text": "answer",
        "mode": "apply",
        "reply_to_message_id": 5,
        "contract_token": dry["contract_token"],
        "idempotency_key": "reply-key",
    }
    with pytest.raises(BridgeError, match="send_readback_failed"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                apply_request,
                inbound_monitor=monitor,
            )
        )

    with pytest.raises(BridgeError, match="send_readback_failed"):
        asyncio.run(telegram_bridge._handle_operation(client, config, apply_request, inbound_monitor=monitor))
    assert monitor.status()["pending_events"] == 1
    assert monitor.status()["reply_verified_events"] == 0
    client.reply_text = "answer"
    replayed = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request, inbound_monitor=monitor))
    assert replayed["replayed"] is True
    assert replayed["verified"] is True
    assert replayed["monitor_event_closed"] is True
    assert monitor.status()["pending_events"] == 0
    assert client.sent_count == 1


def test_verified_reply_reports_when_its_monitor_ref_was_evicted(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    monitor = _inbound_monitor(max_events=1)
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=10, id=5))
    monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=10, id=6))

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def last_message(_client, _entity):
        return 20

    class Client:
        async def send_message(self, _entity, _text, *, reply_to):
            assert reply_to == 5
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            if ids == 5:
                return SimpleNamespace(id=5, message="question", out=False, date=None)
            assert ids == 30
            return SimpleNamespace(message="answer", reply_to_msg_id=5)

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "answer", "mode": "dry_run", "reply_to_message_id": 5},
            inbound_monitor=monitor,
        )
    )
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "send",
                "peer": "10",
                "text": "answer",
                "mode": "apply",
                "reply_to_message_id": 5,
                "contract_token": dry["contract_token"],
                "idempotency_key": "reply-key",
            },
            inbound_monitor=monitor,
        )
    )

    assert applied["reply_verified"] is True
    assert applied["monitor_event_closed"] is False
    assert monitor.status()["dropped_open_events"] == 1


def test_text_reply_rejects_source_changed_after_dry_run(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def last_message(_client, _entity):
        return 20

    class Client:
        source_text = "original question"

        async def get_messages(self, _entity, *, ids):
            assert ids == 5
            return SimpleNamespace(id=5, message=self.source_text, out=False, date=None)

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    request = {
        "operation": "send",
        "peer": "10",
        "text": "answer",
        "mode": "dry_run",
        "reply_to_message_id": 5,
    }
    dry = asyncio.run(telegram_bridge._handle_operation(client, config, request))
    client.source_text = "edited question"

    with pytest.raises(BridgeError, match="contract_reply_source_changed"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                request
                | {
                    "mode": "apply",
                    "contract_token": dry["contract_token"],
                    "idempotency_key": "reply-key",
                },
            )
        )


def test_local_request_transport_and_cli_mapping(monkeypatch, tmp_path, capsys) -> None:
    response_bytes = json.dumps({"ok": True, "authorized": True}).encode() + b"\n"

    class Socket:
        def __init__(self):
            self.response = response_bytes

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            assert timeout == 30

        def connect(self, path):
            assert path == str(tmp_path / "bridge.sock")

        def sendall(self, encoded):
            assert json.loads(encoded) == {"operation": "status"}

        def recv(self, _size):
            value, self.response = self.response, b""
            return value

    monkeypatch.setattr(telegram_bridge.socket, "socket", lambda *_args: Socket())
    result = telegram_bridge.send_local_request(tmp_path / "bridge.sock", {"operation": "status"})
    assert result["authorized"] is True

    requests = []

    def local_request(_socket_path, request):
        requests.append(request)
        return {"ok": True, "request_operation": request["operation"]}

    monkeypatch.setattr(telegram_bridge, "send_local_request", local_request)
    monkeypatch.setitem(
        telegram_bridge.ACCOUNT_PATHS,
        "work",
        telegram_bridge.TelegramAccountPaths(
            credentials_path=tmp_path / "credentials",
            session_path=tmp_path / "state" / "account",
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "bridge.sock",
        ),
    )
    assert telegram_bridge.main(["--account", "work", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["request_operation"] == "status"
    assert (
        telegram_bridge.main(
            [
                "--account",
                "work",
                "resolve-phone",
                "--phone",
                "+79991112233",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert requests[-1] == {"operation": "resolve_phone", "phone": "+79991112233"}
    assert (
        telegram_bridge.main(
            [
                "--account",
                "work",
                "send",
                "--peer",
                "10",
                "--text",
                "answer",
                "--reply-to-message-id",
                "5",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_operation"] == "send"
    assert requests[-1]["reply_to_message_id"] == 5
    assert (
        telegram_bridge.main(
            [
                "--account",
                "work",
                "send-photo",
                "--peer",
                "10",
                "--file",
                str(tmp_path / "frame.jpg"),
                "--caption",
                "caption",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_operation"] == "send_photo"
    assert requests[-1]["caption"] == "caption"


def test_local_request_accepts_only_bounded_timeout_values(monkeypatch, tmp_path) -> None:
    response_bytes = json.dumps({"ok": True}).encode() + b"\n"
    timeouts: list[float] = []

    class Socket:
        def __init__(self):
            self.response = response_bytes

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            timeouts.append(timeout)

        def connect(self, _path):
            return None

        def sendall(self, _encoded):
            return None

        def recv(self, _size):
            value, self.response = self.response, b""
            return value

    monkeypatch.setattr(telegram_bridge.socket, "socket", lambda *_args: Socket())
    assert telegram_bridge.send_local_request(
        tmp_path / "bridge.sock", {"operation": "status"}, timeout_seconds=135
    ) == {"ok": True}
    assert timeouts == [135.0]
    for invalid in (False, 0, 181, "not-a-timeout"):
        with pytest.raises(BridgeError, match="bridge_timeout_invalid"):
            telegram_bridge.send_local_request(
                tmp_path / "bridge.sock", {"operation": "status"}, timeout_seconds=invalid
            )


def test_code_login_keeps_phone_code_and_identity_out_of_result(monkeypatch, tmp_path) -> None:
    events: list[tuple[str, str]] = []
    prompts: list[str] = []

    class Utils:
        @staticmethod
        def parse_phone(value):
            return "79990001122" if value == "test-phone" else None

    class Client:
        def __init__(self, _session, _api_id, _api_hash):
            self.disconnected = False
            self.signed_in = False

        async def connect(self):
            return None

        async def disconnect(self):
            self.disconnected = True

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            events.append(("request", phone))

        async def sign_in(self, *, phone=None, code=None, password=None):
            if code is not None:
                events.append(("code", code))
                assert phone == "79990001122"
                self.signed_in = True
                return SimpleNamespace()
            raise AssertionError(f"unexpected password={password!r}")

        async def get_me(self):
            return SimpleNamespace(id=42, username="private-account") if self.signed_in else None

    values = iter(["test-phone", "test-code"])

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, Utils, RuntimeError))
    result = asyncio.run(telegram_bridge.run_code_login(_runtime_config(tmp_path), read_secret=read_secret))

    assert result == {"ok": True, "authorized": True, "already_authorized": False, "verified": True}
    assert prompts == ["Telegram phone: ", "Telegram login code: "]
    assert events == [("request", "79990001122"), ("code", "test-code")]
    assert "test-phone" not in json.dumps(result)
    assert "test-code" not in json.dumps(result)
    assert "private-account" not in json.dumps(result)


def test_code_login_creates_session_with_private_permissions(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    session_file = config.session_path.with_suffix(".session")

    class Utils:
        @staticmethod
        def parse_phone(_value):
            return "79990001122"

    class Client:
        def __init__(self, _session, _api_id, _api_hash):
            self.signed_in = False

        async def connect(self):
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text("test", encoding="utf-8")

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, _phone):
            return None

        async def sign_in(self, **_kwargs):
            self.signed_in = True

        async def get_me(self):
            return SimpleNamespace() if self.signed_in else None

    values = iter(["test-phone", "test-code"])
    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, Utils, RuntimeError))

    asyncio.run(telegram_bridge.run_code_login(config, read_secret=lambda _prompt: next(values)))

    assert session_file.stat().st_mode & 0o777 == 0o600


def test_code_login_handles_two_factor_with_hidden_prompt(monkeypatch, tmp_path) -> None:
    prompts: list[str] = []

    class PasswordRequired(Exception):
        pass

    class Utils:
        @staticmethod
        def parse_phone(_value):
            return "79990001122"

    class Client:
        def __init__(self, _session, _api_id, _api_hash):
            self.signed_in = False

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, _phone):
            return None

        async def sign_in(self, *, phone=None, code=None, password=None):
            if code is not None:
                raise PasswordRequired
            assert phone is None
            assert password == "test-password"
            self.signed_in = True
            return SimpleNamespace()

        async def get_me(self):
            return SimpleNamespace() if self.signed_in else None

    values = iter(["test-phone", "test-code", "test-password"])

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, Utils, PasswordRequired))
    result = asyncio.run(telegram_bridge.run_code_login(_runtime_config(tmp_path), read_secret=read_secret))

    assert result["verified"] is True
    assert prompts == ["Telegram phone: ", "Telegram login code: ", "Telegram cloud password: "]


def test_code_login_fails_closed_for_an_existing_session(monkeypatch, tmp_path) -> None:
    class Client:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def is_user_authorized(self):
            return True

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (lambda *_args: Client(), object(), RuntimeError))

    with pytest.raises(BridgeError, match="account_already_authorized"):
        asyncio.run(
            telegram_bridge.run_code_login(
                _runtime_config(tmp_path), read_secret=lambda _prompt: pytest.fail("secret prompt was reached")
            )
        )


def test_hidden_terminal_prompt_rejects_noninteractive_input(monkeypatch) -> None:
    class Stream:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(telegram_bridge.sys, "stdin", Stream())
    monkeypatch.setattr(telegram_bridge.sys, "stderr", Stream())

    with pytest.raises(BridgeError, match="interactive_terminal_required"):
        telegram_bridge._read_hidden_terminal_value("Telegram login code: ")


@pytest.mark.parametrize("account", ["personal", "work"])
def test_code_login_command_accepts_both_accounts(monkeypatch, capsys, account) -> None:
    selected_accounts: list[str] = []

    monkeypatch.setattr(
        telegram_bridge,
        "_config_from_args",
        lambda args: SimpleNamespace(account=args.account),
    )

    async def code_login(config):
        selected_accounts.append(config.account)
        return {"ok": True, "authorized": True, "verified": True}

    monkeypatch.setattr(telegram_bridge, "run_code_login", code_login)

    assert telegram_bridge.main(["--account", account, "code-login"]) == 0
    assert selected_accounts == [account]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "authorized": True, "verified": True}


def test_probe_requires_authorization_for_success(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        telegram_bridge,
        "send_local_request",
        lambda _socket, _request: {"ok": True, "authorized": False},
    )

    assert telegram_bridge.main(["--account", "work", "probe"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": True, "authorized": False}


def test_monitor_cli_commands_map_opaque_operations(monkeypatch, capsys) -> None:
    requests: list[dict[str, object]] = []

    def local_request(_socket, request):
        requests.append(request)
        return {"ok": True}

    monkeypatch.setattr(telegram_bridge, "send_local_request", local_request)

    assert telegram_bridge.main(["--account", "work", "monitor-status"]) == 0
    assert telegram_bridge.main(["--account", "work", "monitor-events"]) == 0
    assert telegram_bridge.main(["--account", "work", "monitor-read", "--event-id", "inbound-3"]) == 0
    assert (
        telegram_bridge.main(["--account", "work", "monitor-context", "--event-id", "inbound-3", "--limit", "2"]) == 0
    )
    assert (
        telegram_bridge.main(
            ["--account", "work", "monitor-mark", "--event-id", "inbound-3", "--disposition", "no_reply_needed"]
        )
        == 0
    )

    assert requests == [
        {"operation": "monitor_status"},
        {"operation": "monitor_events"},
        {"operation": "monitor_read", "event_id": "inbound-3"},
        {"operation": "monitor_context", "event_id": "inbound-3", "limit": 2},
        {"operation": "monitor_mark", "event_id": "inbound-3", "disposition": "no_reply_needed"},
    ]
    assert all("send" not in request["operation"] for request in requests)
    assert capsys.readouterr().out.count('"ok": true') == 5


def test_owner_notification_cli_never_accepts_or_emits_a_target(monkeypatch, capsys) -> None:
    requests: list[dict[str, object]] = []

    def local_request(_socket, request):
        requests.append(request)
        return {"ok": True}

    monkeypatch.setattr(telegram_bridge, "send_local_request", local_request)

    assert (
        telegram_bridge.main(
            [
                "--account",
                "work",
                "send-owner-notification",
                "--text",
                "digest",
                "--mode",
                "apply",
                "--contract-token",
                "proof",
                "--idempotency-key",
                "window-1",
            ]
        )
        == 0
    )
    assert (
        telegram_bridge.main(
            [
                "--account",
                "work",
                "owner-notification-readback",
                "--message-id",
                "42",
                "--expected-text-sha256",
                "a" * 64,
            ]
        )
        == 0
    )

    assert requests == [
        {
            "operation": "send_owner_notification",
            "text": "digest",
            "mode": "apply",
            "contract_token": "proof",
            "idempotency_key": "window-1",
        },
        {
            "operation": "owner_notification_readback",
            "message_id": 42,
            "expected_text_sha256": "a" * 64,
        },
    ]
    assert all("peer" not in request and "address" not in request for request in requests)
    assert capsys.readouterr().out.count('"ok": true') == 2


def test_main_enables_inbound_monitor_only_when_explicitly_requested(monkeypatch) -> None:
    selected: list[bool] = []

    async def daemon(_config, *, monitor_incoming):
        selected.append(monitor_incoming)

    monkeypatch.setattr(telegram_bridge, "_config_from_args", lambda args: SimpleNamespace(account=args.account))
    monkeypatch.setattr(telegram_bridge, "run_daemon", daemon)

    assert telegram_bridge.main(["--account", "work", "daemon"]) == 0
    assert telegram_bridge.main(["--account", "personal", "daemon"]) == 0
    assert telegram_bridge.main(["--account", "work", "daemon", "--monitor-incoming"]) == 0
    monkeypatch.setenv(telegram_bridge.WORK_MONITOR_ENVIRONMENT, "1")
    assert telegram_bridge.main(["--account", "work", "daemon"]) == 0
    assert telegram_bridge.main(["--account", "personal", "daemon", "--monitor-incoming"]) == 0
    assert selected == [False, False, True, True, False]


def test_daemon_creates_private_outbox_and_cleans_up(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    state: dict[str, bool] = {}

    class Client:
        def __init__(self, session, api_id, api_hash, *, catch_up):
            assert session == str(config.session_path)
            assert (api_id, api_hash) == (config.api_id, config.api_hash)
            assert catch_up is False

        async def connect(self):
            state["connected"] = True

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            state["disconnected"] = True

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def serve_forever(self):
            state["served"] = True

    async def start_unix_server(callback, *, path):
        assert callable(callback)
        assert path == str(config.socket_path)
        config.socket_path.touch()
        return Server()

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, object(), object()))
    monkeypatch.setattr(telegram_bridge.asyncio, "start_unix_server", start_unix_server)

    asyncio.run(telegram_bridge.run_daemon(config))

    outbox = config.socket_path.parent / "outbox"
    assert outbox.stat().st_mode & 0o777 == 0o700
    assert not config.socket_path.exists()
    assert state == {"connected": True, "served": True, "disconnected": True}


def test_work_daemon_registers_inbound_handler_before_connect(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    state: dict[str, object] = {}
    captured_monitors: list[telegram_bridge.InboundMonitor] = []

    class Client:
        def __init__(self, *_args, **kwargs):
            assert kwargs == {"catch_up": False}
            self.session = SimpleNamespace(save_entities=True)
            state["session"] = self.session

        def add_event_handler(self, callback, builder):
            state["callback"] = callback
            state["builder"] = builder
            state["handler_added_before_connect"] = "connected" not in state

        async def connect(self):
            state["entity_persistence_disabled_before_connect"] = self.session.save_entities is False
            state["connected"] = True

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            state["disconnected"] = True

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def serve_forever(self):
            state["served"] = True

    async def start_unix_server(_callback, *, path):
        assert path == str(config.socket_path)
        config.socket_path.touch()
        return Server()

    events = SimpleNamespace(NewMessage=lambda **kwargs: {"new_message": kwargs})

    async def capture(monitor, event):
        captured_monitors.append(monitor)
        monitor.record(event)

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, object(), object()))
    monkeypatch.setattr(telegram_bridge, "_load_telegram_events", lambda: events)
    monkeypatch.setattr(telegram_bridge, "_capture_incoming_event", capture)
    monkeypatch.setattr(telegram_bridge.asyncio, "start_unix_server", start_unix_server)

    asyncio.run(telegram_bridge.run_daemon(config, monitor_incoming=True))
    asyncio.run(state["callback"](SimpleNamespace(is_private=True, out=False, chat_id=20, id=100, input_chat=object())))

    assert state["handler_added_before_connect"] is True
    assert state["entity_persistence_disabled_before_connect"] is True
    assert state["builder"] == {"new_message": {"incoming": True}}
    assert state["served"] is True
    assert state["disconnected"] is True
    monitor_events = captured_monitors[0].events()
    assert len(monitor_events) == 1
    assert captured_monitors[0].resolve(monitor_events[0]["event_id"]).message_id == 100


def test_rpc_boundary_maps_invalid_and_supported_requests(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)

    class Reader:
        def __init__(self, payload):
            self.payload = payload

        async def readline(self):
            return self.payload

    class Writer:
        def __init__(self):
            self.payload = b""

        def write(self, payload):
            self.payload += payload

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def operation(_client, _config, request, **_kwargs):
        return {"ok": True, "operation": request["operation"]}

    monkeypatch.setattr(telegram_bridge, "_handle_operation", operation)
    writer = Writer()
    asyncio.run(
        telegram_bridge._serve_client(
            object(),
            config,
            Reader(b'{"operation":"status"}\n'),
            writer,
        )
    )
    assert json.loads(writer.payload)["operation"] == "status"

    invalid_writer = Writer()
    asyncio.run(telegram_bridge._serve_client(object(), config, Reader(b"not-json\n"), invalid_writer))
    assert json.loads(invalid_writer.payload) == {"ok": False, "error": "request_invalid"}
