"""Exercise installer pip commands with isolated, non-installing pip fixtures."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def package(root: Path, name: str) -> None:
    module = root / "pip"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__main__.py").write_text(f"print({name!r})\n", encoding="utf-8")


@pytest.mark.parametrize("account,expected", [("work", "runtime"), ("personal", "cwd")])
def test_installer_pip_uses_safe_path_only_for_work(tmp_path: Path, account: str, expected: str) -> None:
    source = (ROOT / "scripts/install-telegram-bridge.sh").read_text(encoding="utf-8")
    case_start = source.index('case "${account}" in')
    case_end = source.index("\nesac", case_start) + len("\nesac")
    root_start = source.index('"${venv_root}/bin/python" -m pip install ')
    root_end = source.index("\nif ! validate_bridge_imports; then", root_start)
    sudo_start = source.index('  sudo -u "${service_user}" env ')
    sudo_end = source.index("\n", source.index('--wheel-dir "${wheel_staging_dir}/out"', sudo_start))

    runtime = tmp_path / "runtime"
    cwd = tmp_path / "cwd"
    package(runtime, "runtime")
    package(cwd, "cwd")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text(
        '#!/bin/sh\nshift 2\nexec /usr/bin/env -i PATH="$PATH" PYTHONPATH="$TEST_RUNTIME" "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    dummy = tmp_path / "unused"
    setup = (
        "set -eu\npython_safety_env=()\n"
        + f"account={account}\n"
        + source[case_start:case_end]
        + '\nvenv_root="$TEST_VENV"\n'
        + 'build_requirements_lock="$TEST_DUMMY"\npyaes_wheel_lock="$TEST_DUMMY"\n'
        + 'wheelhouse_root="$TEST_DUMMY"\nrequirements_lock="$TEST_DUMMY"\n'
        + 'wheel_staging_dir="$TEST_DUMMY"\nstaged_source_lock="$TEST_DUMMY"\n'
        + "chmod() { :; }\nbuild_pyaes_wheelhouse() { :; }\n"
    )
    # Only the exact pip command blocks are executed: all modules are fixture
    # packages, and no installer, dependency installation or live paths are used.
    script = setup + source[root_start:root_end] + "\n" + source[sudo_start:sudo_end] + "\n"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYTHONPATH": str(runtime),
        "TEST_RUNTIME": str(runtime),
        "TEST_VENV": str(venv),
        "TEST_DUMMY": str(dummy),
    }
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [expected] * 5


@pytest.mark.parametrize("existing", [False, True])
def test_work_installer_rejects_unsupported_python_before_unsafe_execution(tmp_path: Path, existing: bool) -> None:
    result, calls = version_preflight(tmp_path, account="work", existing=existing, supported=False)
    assert result.returncode == 1
    assert "telegram_dependency_platform_unsupported=true" in result.stderr
    assert len(calls) == 1
    assert calls[0].startswith("1|-I -S -c ")
    assert "-m venv" not in "\n".join(calls)


@pytest.mark.parametrize("account", ["work", "personal"])
def test_installer_creates_venv_after_work_preflight(tmp_path: Path, account: str) -> None:
    result, calls = version_preflight(tmp_path, account=account, existing=False, supported=True)
    assert result.returncode == 0, result.stderr
    if account == "work":
        assert len(calls) == 2
        assert calls[0].startswith("1|-I -S -c ")
        assert calls[1].startswith("1|-m venv ")
    else:
        assert len(calls) == 1
        assert calls[0].startswith("unset|-m venv ")


def version_preflight(
    tmp_path: Path, *, account: str, existing: bool, supported: bool
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    source = (ROOT / "scripts/install-telegram-bridge.sh").read_text(encoding="utf-8")
    case_start = source.index('case "${account}" in')
    case_end = source.index("\nesac", case_start) + len("\nesac")
    venv_start = source.index('if [[ ! -x "${venv_root}/bin/python" ]]; then')
    preflight_start = source.rfind('if [[ "${account}" == "work" ]]; then\n  installer_python=', 0, venv_start)
    # The baseline has no preflight; run the original venv block in that case.
    if preflight_start < 0:
        preflight_start = venv_start
    end = source.index('\nif [[ "$("${venv_root}/bin/python" -c', venv_start)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        '#!/bin/sh\nprintf "%s|%s\\n" "${PYTHONSAFEPATH-unset}" "$*" >> "$TEST_LOG"\n'
        'if [ "$1" = -I ]; then exit "$TEST_VERSION_STATUS"; fi\n'
        'if [ "$1" = -m ] && [ "$2" = venv ]; then exit 0; fi\nexit 9\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    venv = tmp_path / "venv"
    if existing:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(fake_python)
    log = tmp_path / "python.log"
    script = (
        "set -eu\npython_safety_env=()\n"
        + f"account={account}\n"
        + source[case_start:case_end]
        + '\nvenv_root="$TEST_VENV"\n'
        + source[preflight_start:end]
        + "\n"
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_VENV": str(venv),
        "TEST_LOG": str(log),
        "TEST_VERSION_STATUS": "0" if supported else "1",
    }
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=15)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls
