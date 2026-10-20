"""Runtime wrappers follow systemd drop-ins after a release/runtime migration."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def shell(path, text):
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


@pytest.fixture
def runtime(tmp_path):
    releases = tmp_path / "manager-releases"
    release = releases / "revision"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    current = releases / "current"
    current.symlink_to(release)
    python = tmp_path / "active-venv/bin/python"
    python.parent.mkdir(parents=True)
    shell(python, f'exec {shlex.quote(sys.executable)} "$@"\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shell(
        fake_bin / "systemctl",
        'case "$*" in\n'
        '  *ExecStart*) case "$*" in *wake.service*) module=telegram_wake;; *) module=telegram_bridge;; esac\n'
        '    printf "{ path=%s ; argv[]=instant -B -m autostop_manager.%s daemon ; ignore_errors=no ; }\\n" "$ACTIVE_PYTHON" "$module";;\n'
        "  is-active*) exit 0;;\n"
        "  *) exit 9;;\n"
        "esac\n",
    )
    shell(
        fake_bin / "sudo",
        'printf "%s\\n" \'{"ok":true,"transport_ready":true,"inbound_enabled":true}\'\n',
    )
    shell(fake_bin / "systemd-run", 'printf "%s\\n" "$@" > "$COMMAND_LOG"\nprintf \'{"ok":true}\\n\'\n')
    for name in (
        "work-telegram-python.sh",
        "set-work-telegram-duty.sh",
        "run-work-telegram-media.sh",
        "run-work-telegram-monitor-voice.sh",
    ):
        source = (ROOT / "scripts" / name).read_text()
        source = source.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then")
        source = source.replace("/opt/autostop-work-telegram-releases/current", str(current))
        source = source.replace("/opt/autostop-manager-releases/*", f"{releases}/*")
        source = source.replace("/run/autostop-work-telegram-control.lock", str(tmp_path / "lock"))
        source = source.replace("/etc/autostop-work-telegram/monitor.env", str(tmp_path / "monitor.env"))
        source = source.replace("/etc/autostop-work-telegram/wake.json", str(tmp_path / "wake.json"))
        (scripts / name).write_text(source)
    # No legacy venv or separate work-Telegram release tree exists in this fixture.
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ACTIVE_PYTHON": str(python),
        "COMMAND_LOG": str(tmp_path / "command"),
    }
    return scripts, env, python, tmp_path


@pytest.mark.parametrize("role", ["bridge", "wake"])
def test_python_uses_effective_systemd_runtime(runtime, role):
    scripts, env, python, _ = runtime
    result = subprocess.run(
        ["bash", str(scripts / "work-telegram-python.sh"), role], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    # Keep the virtualenv entry point: resolving its symlink would lose site-packages.
    assert result.stdout.strip() == str(python)


def test_missing_service_runtime_fails_without_a_checkout_fallback(runtime):
    scripts, env, python, _ = runtime
    python.unlink()
    result = subprocess.run(
        ["bash", str(scripts / "work-telegram-python.sh"), "bridge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "work_telegram_runtime_unavailable=true"
    assert not result.stdout


def test_duty_status_uses_active_runtime_without_the_old_venv(runtime):
    scripts, env, _, _ = runtime
    result = subprocess.run(
        ["bash", str(scripts / "set-work-telegram-duty.sh"), "--status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["transport_ready"] is True


def test_media_keeps_its_sandbox_and_uses_active_python(runtime):
    scripts, env, python, tmp_path = runtime
    result = subprocess.run(
        ["bash", str(scripts / "run-work-telegram-media.sh"), "self-check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    command = (tmp_path / "command").read_text().splitlines()
    assert str(python) in command
    assert "autostop_manager.telegram_transcribe" in command
    assert "--self-check" in command
    assert "--property=PrivateNetwork=true" in command
    assert any("InaccessiblePaths=-/var/lib/autostop-work-telegram" in arg for arg in command)


def test_voice_self_check_executes_with_the_active_release_and_runtime(runtime):
    scripts, env, python, tmp_path = runtime
    shell(python, 'printf "%s\\n" "$PYTHONPATH" "$@" > "$COMMAND_LOG"\nprintf \'{"ok":true}\\n\'\n')
    result = subprocess.run(
        ["bash", str(scripts / "run-work-telegram-monitor-voice.sh"), "--self-check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "command").read_text().splitlines() == [
        str(scripts.parent),
        "-m",
        "autostop_manager.telegram_monitor_voice",
        "--self-check",
    ]
