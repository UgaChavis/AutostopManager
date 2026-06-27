from __future__ import annotations

import json

import autostop_manager.knowledge_intake as knowledge_intake
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


def test_knowledge_intake_blocks_outside_project_without_sampling_or_absolute_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = tmp_path / "outside_notes.md"
    source.write_text("LEAKED-ROUTE-TOKEN", encoding="utf-8")
    knowledge_map = root / "knowledge_map.json"
    knowledge_map.write_text(
        json.dumps({"domains": {"leaked_domain": {"keywords": ["LEAKED-ROUTE-TOKEN"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_intake, "KNOWLEDGE_MAP_PATH", knowledge_map)

    draft = build_knowledge_intake_plan(source, project_root=root)
    serialized = json.dumps(draft, ensure_ascii=False)

    assert draft["domain"] == "knowledge_intake"
    assert draft["source_path"] == "<outside_project>/outside_notes.md"
    assert "outside_project" in draft["safety_flags"]
    assert str(source) not in serialized
    assert all(update.get("blocked") is True for update in draft["target_updates"])
    assert all("source_path" not in update for update in draft["target_updates"])


def test_knowledge_intake_blocks_unsafe_metadata_update_paths(tmp_path):
    root = tmp_path / "repo"
    private_file = root / "data" / "private_knowledge" / "customer_email.txt"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("customer mail payload", encoding="utf-8")

    draft = build_knowledge_intake_plan(private_file, project_root=root)

    assert "private_path" in draft["safety_flags"]
    assert "raw_crm_email_or_finance_risk" in draft["safety_flags"]
    assert draft["source_path"] == "data/private_knowledge/customer_email.txt"
    assert all(update["operation"] == "blocked_pending_safety_review" for update in draft["target_updates"])
    assert all("source_path" not in update for update in draft["target_updates"])


def test_knowledge_intake_domain_matching_uses_terms_not_substrings(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "notes.md"
    source.write_text("keyword appears outside normal route terms", encoding="utf-8")
    knowledge_map = root / "knowledge_map.json"
    knowledge_map.write_text(
        json.dumps(
            {
                "domains": {
                    "word_domain": {"keywords": ["word"]},
                    "side_domain": {"keywords": ["side"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_intake, "KNOWLEDGE_MAP_PATH", knowledge_map)

    draft = build_knowledge_intake_plan(source, project_root=root)

    assert draft["domain"] == "knowledge_intake"
