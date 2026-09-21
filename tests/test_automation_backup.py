from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from autostop_manager.automation_registry import AutomationStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/backup-manager-automation-state.py"


def test_online_backup_is_private_atomic_and_component_versioned(tmp_path: Path):
    source = tmp_path / "registry.sqlite3"
    AutomationStore(source).initialize()
    source.chmod(0o600)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    output = backup_dir / "before-release.sqlite3"

    result = subprocess.run(
        [str(SCRIPT), "--source", str(source), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["format"] == "autostop_manager_automation_backup_v1"
    assert report["schema_version"] == 6
    assert output.stat().st_mode & 0o777 == 0o600
    assert not any(path.name.endswith(".tmp") for path in backup_dir.iterdir())
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_backup_refuses_existing_output_and_symlink_source(tmp_path: Path):
    source = tmp_path / "registry.sqlite3"
    AutomationStore(source).initialize()
    source.chmod(0o600)
    link = tmp_path / "registry-link.sqlite3"
    link.symlink_to(source)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    output = backup_dir / "state.sqlite3"

    symlink_result = subprocess.run(
        [str(SCRIPT), "--source", str(link), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output.write_text("preserve", encoding="utf-8")
    existing_result = subprocess.run(
        [str(SCRIPT), "--source", str(source), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert symlink_result.returncode == 1
    assert json.loads(symlink_result.stdout)["error"] == "automation_backup_source_invalid"
    assert existing_result.returncode == 1
    assert json.loads(existing_result.stdout)["error"] == "automation_backup_output_exists"
    assert output.read_text(encoding="utf-8") == "preserve"
    assert os.path.islink(link)
