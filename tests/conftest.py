from __future__ import annotations

import json
from pathlib import Path

import pytest

import autostop_manager.runtime_policy as runtime_policy


@pytest.fixture
def enabled_store_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "docs" / "agent" / "manager_rules.json").read_text(encoding="utf-8"))
    payload["runtime_policies"]["autostop_store"]["state"] = "enabled"
    path = tmp_path / "manager_rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runtime_policy, "MANAGER_RULES_PATH", path)
