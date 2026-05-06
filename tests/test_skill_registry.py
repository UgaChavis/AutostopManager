from __future__ import annotations

from autostop_manager.skill_registry import audit_skill_registry, load_skill_registry


def test_skill_registry_links_known_model_specific_skills():
    registry = load_skill_registry()

    assert registry["ok"] is True
    skill_ids = {skill["skill_id"] for skill in registry["skills"]}
    assert "bmw-f15-n63" in skill_ids
    assert "toyota-gr-yaris" in skill_ids


def test_skill_audit_reports_linked_existing_skills():
    result = audit_skill_registry()

    assert result["ok"] is True
    by_id = {item["skill_id"]: item for item in result["items"]}
    assert by_id["bmw-f15-n63"]["exists"] is True
    assert by_id["bmw-f15-n63"]["linked_domains"]
    assert by_id["toyota-gr-yaris"]["exists"] is True
    assert by_id["toyota-gr-yaris"]["linked_domains"]
