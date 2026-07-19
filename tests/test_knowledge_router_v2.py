from __future__ import annotations

import autostop_manager.knowledge_base as knowledge_base
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


def test_board_cleanup_route_has_single_canonical_alias():
    route = find_command_route("Приберись")

    assert route is not None
    assert route["aliases"] == ["Приберись"]


def test_store_analytics_natural_query_routes_to_aggregate_playbook(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "какие товары смотрели за неделю", limit=5)

    assert result["ok"] is True
    assert result["best_domain"] == "store_analytics_reporting"
    assert result["open_first"] == "docs/agent/store_analytics_playbook.md"
    assert result["command_route"]["command_id"] == "store_analytics_reporting"


def test_noncanonical_cleanup_words_are_not_command_aliases():
    assert find_command_route("оформи карточку") is None
    assert find_command_route("обнови описание CRM") is None


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


def test_probe_routes_timer_floor_wording_with_more_than_two_days(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "проанализируй все активные карточки CRM и сделай всем карточкам таймер более двух суток",
        limit=5,
    )

    assert result["best_domain"] == "service_management"
    assert result["command_route"]["command_id"] == "timer_floor_control"
    assert result["open_first"].endswith("crm_manager_data_playbook.md")


def test_gateway_routing_fix_query_stays_in_project_engineering(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "исправить маршрутизацию Gateway v2 для bulk_set_deadline_if_below, Action Contract и dry_run metadata",
        limit=5,
    )

    assert result["best_domain"] == "startup_and_identity"
    assert result["open_first"] == "AGENTS.md"


def test_probe_routes_gmail_connector_work_to_gmail_operations(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "проверить Gmail коннектор почта ярлыки вложения черновики", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "gmail_operations"
    assert result["open_first"].endswith("gmail_workflow_playbook.md")


def test_probe_routes_project_refactoring_to_startup_sources(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "архитектура, системный аудит, тестирование, рефакторинг и поиск дефектов AutoStop Manager",
        limit=5,
    )

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "startup_and_identity"
    assert result["open_first"] == "AGENTS.md"


def test_project_engineering_hint_does_not_capture_automotive_fault_code_tests():
    hints = knowledge_base._domain_hints("код ошибки P0171 тест датчика кислорода")

    assert "startup_and_identity" not in hints


def test_store_owner_phrases_route_to_store_playbook_without_parts_or_labor_misrouting(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    phrases = [
        "Что нового появилось в магазине?",
        "Что нового сегодня в магазине?",
        "Есть ли новые заказы?",
        "Покажи активные заказы магазина",
        "Что заказали в заказе №123?",
        "Покажи новые заявки на подбор",
        "На магазин запчастей пришел новый заказ на проценку",
        "Покажи запрос на проценку",
        "Прочитай заявку на проценку и найди запчасти",
        "Что запрашивают в заявке на проценку?",
        "Найди запчасть W 914/2 в нашем каталоге",
        "Сколько этой детали на складе?",
        "Где она лежит?",
        "Сколько физически, зарезервировано и доступно?",
        "Покажи последние приходы и отгрузки",
        "Какие позиции заканчиваются?",
        "Покажи состояние склада",
        "Покажи ошибки выгрузки Avito/Drom",
    ]

    for phrase in phrases:
        route = find_command_route(phrase)
        result = probe_knowledge_base(store, phrase, limit=5)
        assert route is not None, phrase
        assert route["workflow_id"] == "store_read_workflow", phrase
        assert result["best_domain"] == "store_management", phrase
        assert result["open_first"] == "docs/agent/store_management_playbook.md", phrase
        assert result["best_domain"] not in {"parts_sourcing", "work_labor_pricing"}, phrase


def test_store_write_phrases_route_to_allowlisted_management_workflow():
    phrases = [
        "назначь заявку на подбор сотруднику",
        "переведи заявку на подбор в работу",
        "верни заявку на подбор в NEW",
        "обнови внутренний комментарий заявки",
        "добавь комментарий в заявку на проценку",
        "добавь заметку в заявку на проценку",
        "подготовь черновики для заявки на проценку",
        "измени место хранения партии",
        "переведи заказ магазина в READY",
    ]

    for phrase in phrases:
        route = find_command_route(phrase)
        assert route is not None, phrase
        assert route["workflow_id"] == "store_management_workflow", phrase


def test_general_drom_sourcing_and_crm_repair_order_stay_outside_store_route(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    public_parts = probe_knowledge_base(store, "найди рулевую рейку на Drom в Красноярске", limit=5)
    repair_order = probe_knowledge_base(store, "обнови материалы и оплату в заказ-наряде CRM", limit=5)

    assert public_parts["best_domain"] == "parts_sourcing"
    assert public_parts["command_route"] is None
    assert repair_order["best_domain"] != "store_management"
    assert (repair_order["command_route"] or {}).get("workflow_id") != "store_read_workflow"


def test_store_today_route_documents_krasnoyarsk_business_time_and_opaque_utc_checkpoint():
    route = find_command_route("Что нового сегодня в магазине?")
    playbook = (knowledge_base.PROJECT_ROOT / "docs" / "agent" / "store_management_playbook.md").read_text(
        encoding="utf-8"
    )

    assert route is not None
    assert route["workflow_id"] == "store_read_workflow"
    assert any("Asia/Krasnoyarsk" in check and "UTC" in check for check in route["completion_checks"])
    assert "Asia/Krasnoyarsk" in playbook
    assert "technical UTC timestamp" in playbook
    assert "cursor is opaque" in playbook
