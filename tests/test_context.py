from __future__ import annotations

from autostop_manager import context
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

    result = prepare_manager_context(store, "Приберись", intent="board_cleanup", limit=8)

    assert result["ok"] is True
    assert result["intent"] == "board_cleanup"
    assert result["command_route"]["command_id"] == "board_cleanup_autopilot"
    assert result["knowledge"]["best_domain"] == "board_cleanup_autopilot"
    assert result["knowledge"]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert any(
        item["kind"] == "rule" and item["title"] == "board-cleanup-no-card-movement"
        for item in result["relevant_memory"]
    )
    assert any(item["kind"] == "fact" and item["category"] == "owner_preference" for item in result["relevant_memory"])
    assert "read live CRM board state with agent_bootstrap and agent_board_digest" in result["next_actions"]
    assert any("cleanup_card dry_run/apply" in action for action in result["next_actions"])


def test_prepare_manager_context_flags_missing_required_context(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = prepare_manager_context(store, "BMW F15 N63 BDC fault", limit=5)

    assert result["ok"] is True
    assert result["knowledge"]["best_domain"] == "bmw_f15_n63"
    assert "VIN or chassis" in result["missing_context"]


def test_agent_brief_routes_compact_project_engineering_query_to_startup(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(
        store,
        "полный рефакторинг поиск багов отладка всего AutoStopManager",
        limit=8,
    )

    assert result["route"]["domain"] == "startup_and_identity"
    assert result["route"]["open_first"] == "AGENTS.md"


def test_build_agent_brief_returns_compact_board_cleanup_start_package(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)

    result = context.build_agent_brief(store, "Приберись", intent="board_cleanup", limit=8)

    assert result["ok"] is True
    assert result["format"] == "agent_brief_v1"
    assert result["role"] == "AutoStop CRM manager agent"
    assert result["memory_sources"] == {
        "local_sqlite": "knowledge_index_and_local_rules",
        "crm_mcp": "operational_memory_and_live_board_context",
        "rule": "before CRM work, read live MCP context; before broad docs, use local knowledge routes",
    }
    assert result["route"]["domain"] == "board_cleanup_autopilot"
    assert result["route"]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert len(result["hot_rules"]) <= 8
    assert any("CRM" in rule and "source of truth" in rule for rule in result["hot_rules"])
    assert any("vehicle passport and client data" in rule for rule in result["hot_rules"])
    assert any("very short formatted summary" in rule for rule in result["hot_rules"])
    assert any("empty leave it empty" in rule for rule in result["hot_rules"])
    assert any("phone is the primary client match key" in rule for rule in result["hot_rules"])
    assert any("rare operational tags capped at three" in rule for rule in result["hot_rules"])
    assert "agent_bootstrap" in result["read_order"][0]
    assert any("audit_client_links" in action for action in result["read_order"])
    assert any("board_summary" in action for action in result["allowed_actions"])
    assert any("client" in action and "vehicle" in action for action in result["allowed_actions"])
    assert any("direct safe card tasks" in action for action in result["allowed_actions"])
    assert any("at most three" in action and "tags" in action for action in result["allowed_actions"])
    assert any("phone/VIN/plate/mileage" in action for action in result["allowed_actions"])
    assert any("move" in action for action in result["forbidden_actions"])
    assert any("archive" in action for action in result["forbidden_actions"])
    assert any("delete" in action for action in result["forbidden_actions"])
    assert any("payments" in action and "repair-order" in action for action in result["forbidden_actions"])
    assert any("client" in action and "merge" in action for action in result["forbidden_actions"])
    assert any("empty public description" in action for action in result["forbidden_actions"])
    assert any("verified structured transfer" in action for action in result["forbidden_actions"])
    assert any("board_summary_stale=false" in check for check in result["verification"])
    assert any("payments_changed=0" in check for check in result["verification"])
    assert result["context_safety"]["checkpoint_event_types"] == [
        "planned_action",
        "checkpoint",
        "skip",
        "write",
        "risk",
        "verification",
    ]
    assert any("workflow_status" in step for step in result["context_safety"]["recovery"])
    assert any("raw board snapshots" in rule for rule in result["context_safety"]["rules"])


def test_agent_brief_exposes_optional_runtime_catalog_cache_and_part_context(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(
        store,
        "в карточке CRM VIN найти OEM фильтра и сверить по локальному каталогу PDF",
        limit=8,
    )

    assert result["ok"] is True
    assert result["route"]["domain"] == "crm_vin_oem_parts_lookup"
    assert "requested part" not in result["missing_context"]
    assert "data/offline_parts_catalogs/catalog_index.json" in result["route"]["optional_runtime_files"]
    assert "reference_files" in result["route"]


def test_prepare_context_uses_focused_memory_for_vin_oem_parts_lookup(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)
    store.remember(
        "Когда владелец просит оформить описание карточки, нужно не мешать это с VIN/OEM подбором.",
        kind="note",
        title="Оформление описаний карточек",
        category="crm_style",
        tags=["crm", "vin"],
        importance=5.0,
    )
    store.remember(
        "Приберись: проверять CRM карточки, VIN и оформление описаний.",
        kind="note",
        title="Приберись: formatted descriptions and vehicle passport",
        category="board_cleanup",
        tags=["crm", "vin"],
        importance=5.0,
    )

    result = prepare_manager_context(
        store,
        "в карточке CRM по VIN найди OEM каталожный номер фильтра и аналоги",
        limit=8,
    )

    assert result["knowledge"]["best_domain"] == "crm_vin_oem_parts_lookup"
    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "") for item in result["relevant_memory"]
    ).casefold()
    assert "vin-oem-lookup-workflow" in context_text
    assert "board-cleanup" not in context_text
    assert "приберись" not in context_text
    assert "оформление описаний" not in context_text
