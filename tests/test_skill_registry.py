from __future__ import annotations

from pathlib import Path

import autostop_manager.skill_registry as skill_registry
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


def test_skill_registry_flags_invalid_knowledge_map(tmp_path, monkeypatch):
    knowledge_map_path = tmp_path / "knowledge_map.json"
    knowledge_map_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(skill_registry, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    registry = load_skill_registry(skill_root=tmp_path / "skills")
    audit = audit_skill_registry(skill_root=tmp_path / "skills")

    assert registry["ok"] is False
    assert registry["load_error"] == "knowledge_map_invalid_structure"
    assert audit["ok"] is False
    assert "knowledge_map_load_error: knowledge_map_invalid_structure" in audit["warnings"]


def test_skill_registry_handles_unreadable_knowledge_map(tmp_path, monkeypatch):
    knowledge_map_path = tmp_path / "knowledge_map.json"
    knowledge_map_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(skill_registry, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    def fake_read_text(self, encoding="utf-8-sig"):
        raise OSError("permission denied")

    monkeypatch.setattr(skill_registry.Path, "read_text", fake_read_text)

    registry = load_skill_registry(skill_root=tmp_path / "skills")
    audit = audit_skill_registry(skill_root=tmp_path / "skills")

    assert registry["ok"] is False
    assert registry["load_error"] == "knowledge_map_unreadable"
    assert audit["ok"] is False
    assert "knowledge_map_load_error: knowledge_map_unreadable" in audit["warnings"]


def test_skill_registry_rejects_unsafe_knowledge_map_skill_path(tmp_path, monkeypatch):
    outside_root = tmp_path / "outside"
    unsafe_skill_path = outside_root / "demo" / "SKILL.md"
    unsafe_skill_path.parent.mkdir(parents=True)
    unsafe_skill_path.write_text("# Demo\n", encoding="utf-8")
    knowledge_map_path = tmp_path / "knowledge_map.json"
    knowledge_map_path.write_text(
        ('{"domains":{"demo":{"source_of_truth_files":["' + str(unsafe_skill_path) + '"]}}}'),
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_registry, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    registry = load_skill_registry(skill_root=tmp_path / "skills")
    audit = audit_skill_registry(skill_root=tmp_path / "skills")

    assert registry["skills"][0]["path"] == "<unsafe_skill_path>/SKILL.md"
    assert registry["skills"][0]["unsafe_path"] == "outside_allowed_skill_roots"
    assert audit["ok"] is False
    assert "unsafe skill path: demo" in audit["warnings"]
    assert audit["items"][0]["exists"] is False
