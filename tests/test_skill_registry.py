from __future__ import annotations

from pathlib import Path

from autostop_manager.skill_registry import SKILL_ROOT, audit_skill_registry, load_skill_registry


def test_skill_registry_uses_current_user_skill_root():
    registry = load_skill_registry()

    assert registry["ok"] is True
    assert Path(registry["skill_root"]).name == "skills"
    assert Path(registry["skill_root"]) == SKILL_ROOT


def test_skill_registry_tolerates_missing_current_user_skill_root(tmp_path):
    missing_root = tmp_path / "missing" / "skills"

    registry = load_skill_registry(skill_root=missing_root)
    result = audit_skill_registry(skill_root=missing_root)

    assert registry["ok"] is True
    assert Path(registry["skill_root"]) == missing_root
    assert result["ok"] is True


def test_skill_audit_does_not_require_retired_local_skills():
    result = audit_skill_registry()

    assert result["ok"] is True
    assert all(not warning.startswith("missing skill file") for warning in result["warnings"])
