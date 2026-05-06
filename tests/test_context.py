from __future__ import annotations

from autostop_manager.context import prepare_manager_context
from autostop_manager.knowledge_base import sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def test_prepare_manager_context_combines_rules_memory_and_knowledge_route(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)
    store.remember(
        "Owner prefers short board-cleanup reports with counts and blockers.",
        kind="fact",
        category="owner_preference",
        tags=["board-cleanup"],
    )

    result = prepare_manager_context(store, "прибейсь", intent="board_cleanup", limit=8)

    assert result["ok"] is True
    assert result["intent"] == "board_cleanup"
    assert result["command_route"]["command_id"] == "board_cleanup_autopilot"
    assert result["knowledge"]["best_domain"] == "board_cleanup_autopilot"
    assert result["knowledge"]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert any(item["kind"] == "rule" and item["title"] == "board-cleanup-no-card-movement" for item in result["relevant_memory"])
    assert any(item["kind"] == "fact" and item["category"] == "owner_preference" for item in result["relevant_memory"])
    assert "read live CRM board state" in result["next_actions"]


def test_prepare_manager_context_flags_missing_required_context(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = prepare_manager_context(store, "BMW F15 N63 BDC fault", limit=5)

    assert result["ok"] is True
    assert result["knowledge"]["best_domain"] == "bmw_f15_n63"
    assert "VIN or chassis" in result["missing_context"]
