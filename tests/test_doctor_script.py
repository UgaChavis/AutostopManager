from __future__ import annotations

import subprocess
import shutil
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


def test_doctor_script_syntax_and_readiness_checks():
    script = ROOT / "scripts" / "doctor.sh"

    content = script.read_text(encoding="utf-8")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this host")

    result = subprocess.run([bash, "-n", str(script)], check=False, capture_output=True)
    output = _decode_output(result.stdout + result.stderr)
    if result.returncode != 0 and _is_unavailable_wsl(output):
        pytest.skip("bash resolves to WSL, but no Linux distribution is installed")

    assert result.returncode == 0, output
    assert "manager MCP import" in content
    assert "crm MCP import" in content
    assert "manager environment report" in content
    assert "crm playwright version" in content
    assert "nginx config" in content
    assert "production watchdog timer disabled" in content
    assert "production watchdog timer inactive" in content
    assert "check_agent_gateway_v2.py" in content
    assert "crm local Gateway v2" in content
    assert "crm public Gateway v2" in content
    assert "crm local MCP initialize" not in content
