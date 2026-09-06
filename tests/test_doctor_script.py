from __future__ import annotations

import subprocess
import shutil
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _decode_output(data: bytes) -> str:
    if b"\x00" in data:
        encodings = ("utf-16-le", "utf-8", "cp866", "cp1251")
    else:
        encodings = ("utf-8", "utf-16-le", "cp866", "cp1251")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_unavailable_wsl(output: str) -> bool:
    lower = output.lower()
    return (
        "windows subsystem for linux" in lower
        or "execvpe(/bin/bash) failed" in lower
        or "wsl.exe --install" in lower
        or "установите дистрибутив" in lower
        or "установленные дистрибутивы" in lower
    )


def _assert_bash_syntax(script: Path) -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this host")

    result = subprocess.run([bash, "-n", str(script)], check=False, capture_output=True)
    output = _decode_output(result.stdout + result.stderr)
    if result.returncode != 0 and _is_unavailable_wsl(output):
        pytest.skip("bash resolves to WSL, but no Linux distribution is installed")

    assert result.returncode == 0, output
    return script.read_text(encoding="utf-8")


def test_doctor_script_syntax_and_readiness_checks():
    content = _assert_bash_syntax(ROOT / "scripts" / "doctor.sh")

    assert "manager MCP import" in content
    assert "crm MCP import" in content
    assert "crm playwright version" in content
    assert "nginx config" in content
    assert "production watchdog timer disabled" in content
    assert "production watchdog timer inactive" in content
    assert "check_agent_gateway_v2.py" in content
    assert "crm local Gateway v2" in content
    assert "crm public Gateway v2" in content
    assert "crm local MCP initialize" not in content


def test_release_gates_script_is_local_disposable_and_non_live():
    script = ROOT / "scripts" / "release-gates.sh"
    content = _assert_bash_syntax(script)

    assert script.stat().st_mode & stat.S_IXUSR
    for marker in (
        "mktemp -d /tmp/autostop-manager-release-gates.",
        'AUTOSTOP_MANAGER_DB="$gate_dir/preflight.sqlite3"',
        'COVERAGE_FILE="$gate_dir/.coverage"',
        'MYPY_CACHE_DIR="$gate_dir/mypy-cache"',
        'RUFF_CACHE_DIR="$gate_dir/ruff-cache"',
        '--basetemp "$gate_dir/pytest"',
        "umask 022",
        "cleanup\ntrap - EXIT\nprintf '\\nrelease_gates_ok=true\\n'",
    ):
        assert marker in content
    for forbidden in (
        "store-conductor-release-gate",
        "/opt/AutostopManager/data/autostop_manager.sqlite3",
        "git fetch",
        "git push",
        "sudo ",
        "systemctl",
        "docker ",
        "curl ",
    ):
        assert forbidden not in content
