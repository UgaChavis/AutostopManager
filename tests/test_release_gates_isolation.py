"""Execute the gate shell with inert callbacks to verify inherited cache isolation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess


def test_release_gates_replace_inherited_j1_runtime_paths(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / ".venv/bin").mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/release-gates.sh", project / "scripts/release-gates.sh"
    )
    log = tmp_path / "callbacks.jsonl"
    fake_python = project / ".venv/bin/python"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "record = {key: os.environ.get(key) for key in "
        "['AUTOSTOP_MANAGER_DB', 'AUTOSTOP_J1_CACHE_DIR', 'AUTOSTOP_J1_VIN_CACHE_DIR', "
        "'AUTOSTOP_J1_VIN_RESEARCH_ENABLED', 'AUTOSTOP_J1_SEARXNG_URL']}\n"
        "with Path(os.environ['REVIEW_GATE_CALLBACK_LOG']).open('a') as log:\n"
        "    log.write(json.dumps(record) + '\\n')\n"
        "print(json.dumps({'ok': True}))\n"
    )
    fake_python.chmod(0o755)
    operational = tmp_path / "synthetic-operational-cache"
    operational.mkdir()
    sentinel = operational / "sentinel.txt"
    sentinel.write_text("preserve synthetic runtime")
    monkeypatch.setenv("REVIEW_GATE_CALLBACK_LOG", str(log))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(operational))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(operational))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "http://synthetic-provider.invalid/")
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True, timeout=5)

    completed = subprocess.run(
        ["bash", str(project / "scripts/release-gates.sh")], check=True, capture_output=True, text=True, timeout=15
    )
    assert "release_gates_ok=true" in completed.stdout
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) >= 10
    for record in records:
        gate_root = Path(record["AUTOSTOP_MANAGER_DB"]).parent
        ordinary = Path(record["AUTOSTOP_J1_CACHE_DIR"])
        private = Path(record["AUTOSTOP_J1_VIN_CACHE_DIR"])
        assert ordinary.parent == private.parent == gate_root
        assert ordinary != private and operational not in (ordinary, private)
        assert record["AUTOSTOP_J1_VIN_RESEARCH_ENABLED"] == "0"
        assert record["AUTOSTOP_J1_SEARXNG_URL"] == ""
    assert sentinel.read_text() == "preserve synthetic runtime"
    assert not gate_root.exists()
