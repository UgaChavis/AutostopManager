"""Work-only wrappers must import the selected runtime rather than the caller's cwd."""

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_work_model_validation_ignores_caller_module_after_sudo_reset(tmp_path: Path) -> None:
    source = (ROOT / "scripts/provision-telegram-transcription-model.sh").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^validate_target_runtime\(\) \{\n.*?^\}", source)
    assert match is not None
    target_modules = tmp_path / "target-modules"
    target_modules.mkdir()
    selected = tmp_path / "selected"
    (target_modules / "faster_whisper.py").write_text(
        f"from pathlib import Path\nPath({str(selected)!r}).touch()\n"
        "class WhisperModel:\n    def __init__(self, *args, **kwargs): pass\n",
        encoding="utf-8",
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    hostile = tmp_path / "cwd-imported"
    (cwd / "faster_whisper.py").write_text(
        f"from pathlib import Path\nPath({str(hostile)!r}).touch()\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    python = tmp_path / "python"
    shell(python, f'export PYTHONPATH={shlex.quote(str(target_modules))}\nexec {shlex.quote(sys.executable)} "$@"\n')
    script = 'sudo() { shift 2; /usr/bin/env -i PATH="$PATH" "$@"; };\n' + match[0] + "\nvalidate_target_runtime\n"
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={**os.environ, "target_user": "fake", "target_venv": str(python), "target_model": str(tmp_path / "model")},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert selected.exists()
    assert not hostile.exists()


def shell(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def package(base: Path, module: str, source: str) -> None:
    directory = base / "autostop_manager"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / f"{module}.py").write_text(source, encoding="utf-8")


@pytest.mark.parametrize("scenario", ["probe", "code-login"])
def test_work_authorization_ignores_caller_package(tmp_path: Path, scenario: str) -> None:
    release = tmp_path / "release"
    cwd = tmp_path / "cwd"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "selected-runtime"
    hostile_marker = tmp_path / "caller-imported"
    authorized = scenario == "probe"
    package(
        release,
        "telegram_bridge",
        f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text(sys.argv[-1])\n"
        f"print('{{\"authorized\": {str(authorized).lower()}}}')\n"
        "raise SystemExit(4 if sys.argv[-1] == 'code-login' else 0)\n",
    )
    package(
        cwd,
        "telegram_bridge",
        f"import pathlib\npathlib.Path({str(hostile_marker)!r}).touch()\nraise SystemExit(4)\n",
    )
    shell(
        fake_bin / "systemctl",
        'case "$1" in show) echo loaded;; *) exit 0;; esac\n',
    )
    # Drop the environment as sudo does, retaining only this fixture's PATH.
    shell(fake_bin / "sudo", 'shift 2\nexec /usr/bin/env -i PATH="$PATH" "$@"\n')
    source = (ROOT / "scripts/authorize-telegram-account.sh").read_text(encoding="utf-8")
    source = source.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then", 1)
    source = source.replace('release_link="/opt/autostop-work-telegram-releases/current"', f'release_link="{release}"')
    source = source.replace(
        'venv_python="/opt/autostop-work-telegram-venv/bin/python"', f'venv_python="{sys.executable}"'
    )
    source = source.replace(
        'session_base="/var/lib/autostop-work-telegram/account.session"', f'session_base="{tmp_path / "session"}"'
    )
    source = source.replace(
        'monitor_env="/etc/autostop-work-telegram/monitor.env"', f'monitor_env="{tmp_path / "monitor.env"}"'
    )
    source = source.replace(
        'control_lock="/run/autostop-work-telegram-control.lock"', f'control_lock="{tmp_path / "control.lock"}"'
    )
    script = tmp_path / "authorize.sh"
    script.write_text(source, encoding="utf-8")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        ["bash", str(script), "--account", "work"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (0 if authorized else 1), result.stderr
    assert marker.read_text(encoding="utf-8") == scenario
    assert not hostile_marker.exists()


def test_work_voice_wrapper_ignores_caller_package(tmp_path: Path) -> None:
    releases = tmp_path / "manager-releases"
    release = releases / "revision"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    current = releases / "current"
    current.symlink_to(release)
    cwd = tmp_path / "cwd"
    hostile_marker = tmp_path / "caller-imported"
    package(release, "telegram_monitor_voice", 'print("selected-runtime")\n')
    package(cwd, "telegram_monitor_voice", f"import pathlib\npathlib.Path({str(hostile_marker)!r}).touch()\n")
    shell(scripts / "work-telegram-python.sh", f"printf '%s\\n' {shlex.quote(sys.executable)}\n")
    source = (ROOT / "scripts/run-work-telegram-monitor-voice.sh").read_text(encoding="utf-8")
    source = source.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then", 1)
    source = source.replace("/opt/autostop-work-telegram-releases/current", str(current))
    source = source.replace("/opt/autostop-manager-releases/*", f"{releases}/*")
    script = scripts / "voice.sh"
    script.write_text(source, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        ["bash", str(script), "--self-check"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "selected-runtime"
    assert not hostile_marker.exists()
