from __future__ import annotations

from autostop_manager.knowledge_intake import build_knowledge_intake_plan


def test_knowledge_intake_dry_run_classifies_local_metadata():
    draft = build_knowledge_intake_plan("docs/agent/knowledge_map.json")

    assert draft["schema"] == "KnowledgeIntakeDraft"
    assert draft["exists"] is True
    assert draft["classification"]["source_type"] == "structured_metadata"
    assert draft["domain"] == "knowledge_intake"
    assert draft["apply_requested"] is False
    assert draft["apply_result"]["status"] == "dry_run_only"
    assert "knowledge-sync" in draft["required_follow_up"]


def test_knowledge_intake_blocks_private_apply():
    draft = build_knowledge_intake_plan("data/private_knowledge/customer_email.txt", apply=True)

    assert draft["schema"] == "KnowledgeIntakeDraft"
    assert draft["ok"] is False
    assert draft["apply_requested"] is True
    assert draft["apply_allowed"] is False
    assert "private_path" in draft["safety_flags"]
    assert "raw_crm_email_or_finance_risk" in draft["safety_flags"]
    assert draft["privacy"]["raw_private_file_committed"] is False
