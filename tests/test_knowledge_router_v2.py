from __future__ import annotations

from autostop_manager.knowledge_base import find_command_route, probe_knowledge_base, sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def test_probe_routes_owner_board_cleanup_command_to_cleanup_playbook(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Приберись", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert result["command_route"]["command_id"] == "board_cleanup_autopilot"


def test_deprecated_voice_variants_are_not_command_aliases():
    assert find_command_route("прибейсь") is None
    assert find_command_route("переберись") is None


def test_probe_routes_ready_unpaid_daily_control_to_service_management(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "вечером проверь просроченные машины и готовые без оплаты", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "service_management"
    assert result["open_first"].endswith("krasnoyarsk_service_management_playbook.md")
    assert all("без" not in route["matching_terms"] for route in result["routes"])


def test_probe_routes_inbox_triage_to_cleanup_playbook(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "разбери входящие карточки", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["command_route"]["command_id"] == "inbox_triage"


def test_probe_routes_timer_floor_to_manager_data_playbook(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "сделай таймеры на активных карточках не менее двух суток",
        limit=5,
    )

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "service_management"
    assert result["command_route"]["command_id"] == "timer_floor_control"
    assert result["open_first"].endswith("crm_manager_data_playbook.md")


def test_probe_routes_gmail_connector_work_to_gmail_operations(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "проверить Gmail коннектор почта ярлыки вложения черновики", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "gmail_operations"
    assert result["open_first"].endswith("gmail_workflow_playbook.md")
