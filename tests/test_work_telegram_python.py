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
    (tmp_path / "lock").touch(mode=0o600)  # Normally provisioned by systemd-tmpfiles.
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
        '  *MainPID*) printf "%s\\n" "$ACTIVE_PID";;\n'
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
        if name == "work-telegram-python.sh":
            source = source.replace("/opt/autostop-manager-releases", str(releases))
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


@pytest.mark.parametrize("role", ["personal", "bridge", "wake"])
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


def bridge_module(path, body):
    package = path / "autostop_manager"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "telegram_bridge.py").write_text(body)


def test_bridge_launcher_keeps_cwd_and_arguments_but_imports_its_own_pinned_release(runtime):
    scripts, env, python, tmp_path = runtime
    revision = "a" * 40
    release = scripts.parent
    (release / "REVISION").write_text(revision + "\n")
    bridge_module(
        release,
        "import json, os, sys\n"
        "print(json.dumps({'source': __file__, 'cwd': os.getcwd(), 'args': sys.argv[1:],"
        " 'effective_python': os.environ['EFFECTIVE_PYTHON_INVOKED']}))\n",
    )
    old_cwd = tmp_path / "old-manager"
    wrong_pythonpath = tmp_path / "other-imports"
    bridge_module(old_cwd, "raise RuntimeError('used stale working directory')\n")
    bridge_module(wrong_pythonpath, "raise RuntimeError('used stale PYTHONPATH')\n")
    shell(python, f'export EFFECTIVE_PYTHON_INVOKED="$0"\nexec {shlex.quote(sys.executable)} "$@"\n')
    args = ["job-complete-reply", "--receipt-key", 'literal * "quote"\nnext', ""]
    launcher = release.parent / "current" / "scripts" / "work-telegram-python.sh"

    result = subprocess.run(
        ["bash", str(launcher), "bridge", "--run", revision, "--", *args],
        cwd=old_cwd,
        env={**env, "PYTHONPATH": str(wrong_pythonpath), "PYTHONSAFEPATH": "", "PYTHONHOME": str(wrong_pythonpath)},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "source": str(release / "autostop_manager/telegram_bridge.py"),
        "cwd": str(old_cwd),
        "args": ["--account", "work", *args],
        "effective_python": str(python),
    }


def test_bridge_launcher_rejects_mismatched_revision_before_execution(runtime):
    scripts, env, _, _ = runtime
    (scripts.parent / "REVISION").write_text("a" * 40 + "\n")
    bridge_module(scripts.parent, "raise RuntimeError('invalid launcher executed the command')\n")
    result = subprocess.run(
        ["bash", str(scripts / "work-telegram-python.sh"), "bridge", "--run", "b" * 40, "--", "status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "work_telegram_release_mismatch=true" and not result.stdout


@pytest.mark.parametrize("work_aligned", [True, False])
def test_wake_installer_requires_one_release_and_uses_bridge_runtime(runtime, work_aligned):
    scripts, env, python, tmp_path = runtime
    source = (ROOT / "scripts/install-codex-wake.sh").read_text().split("export PYTHONPATH", 1)[0]
    source = source.replace('[[ "${EUID}" -eq 0 ]]', "true")
    work_current = tmp_path / "work-current"
    work_current.symlink_to(scripts.parent if work_aligned else tmp_path)
    source = source.replace("/opt/autostop-work-telegram-releases/current", str(work_current))
    source = source.replace("/opt/autostop-manager-releases/current", str(scripts.parent.parent / "current"))
    source = source.replace("/opt/autostop-manager-releases/*", f"{scripts.parent.parent}/*")
    source = source.replace('[[ "$(stat -c %U "$release")" == root ]]', "true")
    shell(
        tmp_path / "bin/systemctl",
        'case "$*" in\n'
        "  is-active*) exit 3;;\n"
        '  *ExecStart*autostop-work-telegram.service*) printf "{ path=%s ; argv[]=python -m autostop_manager.telegram_bridge daemon ; }\\n" "$ACTIVE_PYTHON";;\n'
        "  *) exit 9;;\n"
        "esac\n",
    )
    result = subprocess.run(
        ["bash", "-c", source + 'printf "%s\\n" "$python"\n'],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if work_aligned:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(python)
    else:
        assert result.returncode == 1
        assert result.stderr.strip() == "codex_wake_release_alias_mismatch=true"
        assert not result.stdout


def test_installed_wake_unit_preserves_effective_venv_entry_point(tmp_path):
    source = (ROOT / "scripts/install-codex-wake.sh").read_text()
    render = (
        '"$python" - "$unit" "$python"'
        + source.split('"$python" - "$unit" "$python"', 1)[1].split("\nPY\n", 1)[0]
        + "\nPY\n"
    )
    unit = tmp_path / "wake.service"
    unit.write_text((ROOT / "deploy/systemd/autostop-codex-wake.service").read_text())
    python = tmp_path / "active-runtime/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", "-c", f"python={shlex.quote(str(python))}\nunit={shlex.quote(str(unit))}\n" + render],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert f"ExecStart={python} -m autostop_manager.telegram_wake daemon" in unit.read_text()
    assert "Environment=PYTHONPATH=/opt/autostop-manager-releases/current" in unit.read_text()


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


@pytest.mark.parametrize("operation", ["--status", "--enable"])
def test_duty_bridge_import_ignores_unsafe_working_directory(runtime, operation):
    scripts, env, _, tmp_path = runtime
    fake_bin = tmp_path / "bin"
    # Simulate sudo's normal environment reset; only arguments to env survive.
    shell(fake_bin / "sudo", 'shift 2\nexec /usr/bin/env -i PATH="$PATH" "$@"\n')
    shell(
        fake_bin / "systemctl",
        'case "$*" in\n'
        '  *ExecStart*) printf "{ path=%s ; argv[]=python -m autostop_manager.telegram_bridge daemon ; }\\n" "$ACTIVE_PYTHON";;\n'
        "  is-active*|enable*|restart*) exit 0;;\n"
        "  *) exit 9;;\n"
        "esac\n",
    )
    shell(fake_bin / "chown", "exit 0\n")
    shell(fake_bin / "sleep", "exit 0\n")
    package = scripts.parent / "autostop_manager"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "telegram_bridge.py").write_text(
        'print(\'{"ok":true,"transport_ready":true,"inbound_enabled":true,'
        '"enabled": true,"retention":"memory_only"}\')\n'
    )
    hostile = tmp_path / "working-directory"
    (hostile / "autostop_manager").mkdir(parents=True)
    (hostile / "autostop_manager" / "__init__.py").write_text("")
    marker = tmp_path / "unsafe-import"
    (hostile / "autostop_manager" / "telegram_bridge.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n"
    )
    result = subprocess.run(
        ["bash", str(scripts / "set-work-telegram-duty.sh"), operation],
        cwd=hostile,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    if operation == "--status":
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


@pytest.mark.parametrize(
    ("name", "arguments", "error"),
    [
        ("run-work-telegram-media.sh", ["self-check"], "media_release_mismatch=true"),
        ("run-work-telegram-monitor-voice.sh", ["--self-check"], "monitor_voice_release_mismatch=true"),
    ],
)
def test_old_pinned_wrapper_refuses_new_current(runtime, name, arguments, error):
    scripts, env, _, tmp_path = runtime
    old_release = scripts.parent.parent / "old-revision"
    old_scripts = old_release / "scripts"
    old_scripts.mkdir(parents=True)
    old_script = old_scripts / name
    old_script.write_text((scripts / name).read_text())

    result = subprocess.run(["bash", str(old_script), *arguments], env=env, capture_output=True, text=True, timeout=15)

    assert result.returncode == 1
    assert error in result.stderr
    assert not (tmp_path / "command").exists()


@pytest.mark.parametrize(
    ("name", "arguments", "error"),
    [
        ("run-work-telegram-media.sh", ["transcribe", "--file", "FILE"], "media_release_mismatch=true"),
        ("run-work-telegram-monitor-voice.sh", ["--event-id", "inbound-1"], "monitor_voice_release_mismatch=true"),
    ],
)
def test_work_requires_same_release_as_running_bridge(runtime, name, arguments, error):
    scripts, env, python, tmp_path = runtime
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    media = inbox / "voice.ogg"
    media.write_text("synthetic")
    path = scripts / name
    path.write_text(path.read_text().replace('inbox_dir="/run/autostop-work-telegram/inbox"', f'inbox_dir="{inbox}"'))
    arguments = [str(media) if value == "FILE" else value for value in arguments]
    shell(python, 'printf "%s\\n" "$PYTHONPATH" "$@" > "$COMMAND_LOG"\nprintf \'{"ok":true}\\n\'\n')
    old_release = scripts.parent.parent / "old-revision"
    old_release.mkdir()

    for process_dir, expected_return in ((old_release, 1), (scripts.parent, 0)):
        bridge = subprocess.Popen(["sleep", "30"], cwd=process_dir)
        try:
            result = subprocess.run(
                ["bash", str(path), *arguments],
                env={**env, "ACTIVE_PID": str(bridge.pid)},
                capture_output=True,
                text=True,
                timeout=15,
            )
        finally:
            bridge.terminate()
            bridge.wait(timeout=5)
        assert result.returncode == expected_return, result.stderr
        if expected_return:
            assert error in result.stderr
            assert not (tmp_path / "command").exists()
        else:
            assert (tmp_path / "command").exists()


def test_standalone_media_wrapper_is_matching_self_check_only(runtime):
    scripts, env, _, tmp_path = runtime
    installed = tmp_path / "sbin/autostop-work-telegram-media"
    installed.parent.mkdir()
    source = scripts / "run-work-telegram-media.sh"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    media = inbox / "voice.ogg"
    media.write_text("synthetic")
    source.write_text(
        source.read_text()
        .replace("/usr/local/sbin/autostop-work-telegram-media", str(installed))
        .replace('inbox_dir="/run/autostop-work-telegram/inbox"', f'inbox_dir="{inbox}"')
    )
    installed.write_text(source.read_text())

    result = subprocess.run(["bash", str(installed), "self-check"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    (tmp_path / "command").unlink()
    result = subprocess.run(
        ["bash", str(installed), "transcribe", "--file", str(media)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "media_release_mismatch=true" in result.stderr
    assert not (tmp_path / "command").exists()
    installed.write_text(installed.read_text() + "\n# stale wrapper\n")
    result = subprocess.run(["bash", str(installed), "self-check"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "media_release_mismatch=true" in result.stderr
    assert not (tmp_path / "command").exists()
