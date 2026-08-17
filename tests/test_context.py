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


def test_agent_brief_for_store_analytics_is_aggregate_only_and_needs_no_clarification(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(store, "сколько посетителей сегодня", limit=8)

    assert result["route"]["domain"] == "store_analytics_reporting"
    assert result["route"]["open_first"] == "docs/agent/store_analytics_playbook.md"
    assert result["missing_context"] == []
    assert any("get_store_analytics_report" in step for step in result["read_order"])
    assert any("aggregate" in action for action in result["allowed_actions"])
    assert any("raw analytics events" in action for action in result["forbidden_actions"])
    assert any("rawEventsIncluded=false" in check for check in result["verification"])


def test_agent_brief_for_general_automotive_repair_selects_sources_adaptively(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(store, "Как выставить ГРМ на Mercedes M274?", limit=8)

    assert result["route"]["domain"] == "automotive_repair"
    assert result["route"]["open_first"] == "docs/agent/automotive_repair_source_playbook.md"
    assert any("AutoStop App" in item for item in result["allowed_actions"])
    assert any("adaptively" in item for item in result["hot_rules"])
    assert any("Forums are hypotheses" in item for item in result["hot_rules"])
    assert any("write CRM" in item for item in result["forbidden_actions"])


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
        "store_api": "live_store_catalog_stock_orders_quotes_and_marketplace_context",
        "rule": "before CRM or store work, read live focused context; before broad docs, use local knowledge routes",
    }
    assert result["route"]["domain"] == "board_cleanup_autopilot"
    assert result["route"]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert len(result["hot_rules"]) <= 8
    assert any("CRM" in rule and "source of truth" in rule for rule in result["hot_rules"])
    assert any("vehicle passport and client data" in rule for rule in result["hot_rules"])
    assert any("coherent evolving history" in rule for rule in result["hot_rules"])
    assert any("one or two natural plain-text sentences" in rule for rule in result["hot_rules"])
    assert any("phone is the primary client match key" in rule for rule in result["hot_rules"])
    assert "agent_bootstrap" in result["read_order"][0]
    assert any("audit_client_links" in action for action in result["read_order"])
    assert any("board_summary" in action for action in result["allowed_actions"])
    assert any("vehicle/client" in action for action in result["allowed_actions"])
    assert any("direct safe card task" in action for action in result["allowed_actions"])
    assert any("move" in action for action in result["forbidden_actions"])
    assert any("archive" in action for action in result["forbidden_actions"])
    assert any("delete" in action for action in result["forbidden_actions"])
    assert any("payments" in action and "repair-order" in action for action in result["forbidden_actions"])
    assert any("merge" in action for action in result["forbidden_actions"])
    assert any("phone, VIN, plate" in action for action in result["forbidden_actions"])
    assert any("board_summary_stale=false" in check for check in result["verification"])
    assert any("payment counts" in check for check in result["verification"])
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


def test_store_agent_brief_exposes_store_source_boundary_and_safe_workflow(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)

    result = context.build_agent_brief(store, "Покажи состояние склада", limit=8)

    assert result["route"]["domain"] == "store_management"
    assert result["route"]["open_first"] == "docs/agent/store_management_playbook.md"
    assert "catalog" in result["source_boundaries"]["store"]
    assert "repair orders" in result["source_boundaries"]["crm"]
    assert any("stateless Store readiness snapshot" in item for item in result["read_order"])
    assert any("agent_search" in item for item in result["read_order"])
    assert any("dedicated quote credential" in item for item in result["read_order"])
    assert any("full quotes are transient" in item for item in result["hot_rules"])
    assert any("store_sourcing_offer" in item for item in result["allowed_actions"])
    assert not any("no contact scope" in item for item in result["hot_rules"])
    assert any("guarded store_owner_api" in item for item in result["forbidden_actions"])
    assert any("store_owner_capabilities" in item for item in result["read_order"])
    assert any("final page" in item for item in result["verification"])
    assert any("AutoStop App" in item and "source of truth" in item for item in result["hot_rules"])


def test_store_agent_brief_exposes_complete_read_and_write_command_selectors(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    read_brief = context.build_agent_brief(store, "Покажи заявки без исполнителя", limit=8)
    write_brief = context.build_agent_brief(store, "очисти внутренний комментарий заявки", limit=8)

    assert set(read_brief["route"]["read_entity_selection"]) == {
        "store_part",
        "store_order",
        "store_quote_request",
        "store_supplier",
        "store_batch",
        "store_warehouse_operation",
        "store_marketplace_listing",
        "store_state",
        "store_sourcing_offer",
    }
    assert set(write_brief["route"]["operation_selection"]) == {
        "assign_quote_request",
        "set_quote_request_status",
        "update_quote_request_comment",
        "add_quote_request_note",
        "replace_quote_offer_drafts",
        "set_batch_storage_location",
        "mark_order_ready",
        "owner_api_fallback",
    }
    comment = write_brief["route"]["operation_selection"]["update_quote_request_comment"]
    note = write_brief["route"]["operation_selection"]["add_quote_request_note"]
    assert "replace or clear" in comment["use_when"]
    assert "append" in note["use_when"]
    assert "employee/admin OpenAPI" in write_brief["route"]["operation_selection"]["owner_api_fallback"]["use_when"]


def test_store_agent_brief_deterministically_selects_each_write_operation(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    phrases = {
        "назначь заявку на подбор сотруднику": "assign_quote_request",
        "переведи заявку на подбор в работу": "set_quote_request_status",
        "очисти внутренний комментарий заявки": "update_quote_request_comment",
        "добавь заметку в заявку на проценку": "add_quote_request_note",
        "очисти приватные черновики предложений": "replace_quote_offer_drafts",
        "переложи партию в ячейку": "set_batch_storage_location",
        "отметь собранный заказ готовым": "mark_order_ready",
    }

    for phrase, operation in phrases.items():
        result = context.build_agent_brief(store, phrase, limit=8)
        assert result["route"]["selected_operation"]["operation"] == operation, phrase


def test_agent_brief_documentation_hygiene_has_safe_cleanup_contract(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(
        store,
        "Обнови документацию, удали мусорные инструкции и закоммить изменения",
    )

    assert result["route"]["domain"] == "knowledge_intake"
    assert result["route"]["command_id"] == "manager_documentation_hygiene"
    assert any("cleanup-audit" in item for item in result["read_order"])
    assert any("unique instruction" in item for item in result["forbidden_actions"])


def test_agent_brief_remote_route_requires_exact_device_and_secret_safety(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = context.build_agent_brief(store, "Проверь managed-pc и удалённый Windows компьютер")

    assert result["route"]["domain"] == "remote_codex_access"
    assert result["route"]["open_first"] == "docs/agent/codex_home_pc_reverse_ssh.md"
    assert any("exact alias" in item for item in result["read_order"])
    assert any("private keys" in item for item in result["forbidden_actions"])


def test_quote_pricing_request_routes_to_store_and_exposes_full_quote_workflow(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)

    result = context.build_agent_brief(
        store,
        "Прочитай новый запрос на проценку, найди запчасти и подготовь комментарий",
        limit=8,
    )

    assert result["route"]["domain"] == "store_management"
    assert any("dedicated quote credential" in item for item in result["read_order"])
    assert any("full quotes are transient" in item for item in result["hot_rules"])
    assert any("store_sourcing_offer" in item for item in result["allowed_actions"])
    assert any("append a note" in item for item in result["allowed_actions"])


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
