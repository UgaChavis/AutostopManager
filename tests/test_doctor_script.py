from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_doctor_script_syntax_and_readiness_checks():
    script = ROOT / "scripts" / "doctor.sh"

    result = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)
    content = script.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "manager MCP import" in content
    assert "crm MCP import" in content
    assert "manager environment report" in content
    assert "crm playwright version" in content
    assert "nginx config" in content
    assert "production watchdog timer active" in content
