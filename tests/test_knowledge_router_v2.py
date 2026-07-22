from __future__ import annotations

import autostop_manager.knowledge_base as knowledge_base
from autostop_manager.context import build_agent_brief
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


def test_documentation_cleanup_does_not_route_to_crm_board_cleanup(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "Обнови документацию Автостоп-менеджера, удали мусорную документацию "
        "и приведи инструкции в актуальное состояние",
        limit=5,
    )

    assert result["best_domain"] == "knowledge_intake"
    assert result["open_first"] == "docs/agent/knowledge_shelves.md"
    assert result["command_route"]["command_id"] == "manager_documentation_hygiene"


def test_remote_windows_management_routes_to_managed_pc_playbook(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "Проверь managed-pc и подключись к удалённому Windows компьютеру",
        limit=5,
    )

    assert result["best_domain"] == "remote_codex_access"
    assert result["open_first"] == "docs/agent/codex_home_pc_reverse_ssh.md"
    assert result["command_route"]["command_id"] == "remote_codex_access"


def test_generic_documentation_cleanup_word_is_not_board_cleanup_hint():
    hints = knowledge_base._domain_hints("documentation cleanup for AutostopManager")

    assert "board_cleanup_autopilot" not in hints
    assert hints["knowledge_intake"] == 90


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


def test_probe_routes_full_ecosystem_parity_to_dedicated_program(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "Полностью отрефакторировать экосистему AutostopManager, получить "
        "функциональный паритет CRM и AutoStop App и production-ready change feed",
        limit=5,
    )

    assert result["ok"] is True
    assert result["best_domain"] == "ecosystem_capability_parity"
    assert result["open_first"] == "AGENTS.md"
    assert result["command_route"]["command_id"] == "ecosystem_capability_parity"


def test_project_engineering_hint_does_not_capture_automotive_fault_code_tests():
    hints = knowledge_base._domain_hints("код ошибки P0171 тест датчика кислорода")

    assert "startup_and_identity" not in hints


def test_general_automotive_technical_queries_route_to_adaptive_repair_sources(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    for query in (
        "Как выставить ГРМ на Mercedes M274?",
        "Какие метки ГРМ и моменты затяжки?",
        "Ошибка P0171",
        "Сколько стоит замена цепи ГРМ на Mercedes?",
    ):
        result = probe_knowledge_base(store, query, limit=5)
        assert result["best_domain"] == "automotive_repair", query
        assert result["open_first"] == "docs/agent/automotive_repair_source_playbook.md", query
        assert result["command_route"] is None, query


def test_automotive_vehicle_and_store_context_are_selected_only_when_present(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    generic_vin = probe_knowledge_base(store, "Подбери фильтр по VIN и аналоги", limit=5)
    internal_store = probe_knowledge_base(store, "Есть ли W 914/2 у нас в магазине?", limit=5)
    crm_writeback = probe_knowledge_base(
        store,
        "В карточке CRM по VIN найди OEM фильтра и запиши в карточку",
        limit=5,
    )

    assert generic_vin["best_domain"] == "vehicle_identity_and_oem"
    assert generic_vin["command_route"] is None
    assert internal_store["best_domain"] == "store_management"
    assert crm_writeback["best_domain"] == "crm_vin_oem_parts_lookup"


def test_model_specific_automotive_routes_need_explicit_vehicle_markers(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    generic_fault = probe_knowledge_base(store, "Ошибка P0171", limit=5)
    bmw = probe_knowledge_base(store, "BMW N63 момент затяжки", limit=5)
    toyota = probe_knowledge_base(store, "Toyota GR Yaris обслуживание", limit=5)

    assert generic_fault["best_domain"] == "automotive_repair"
    assert bmw["best_domain"] == "bmw_repair"
    assert toyota["best_domain"] == "toyota_gr_yaris"


def test_low_confidence_generic_interrogative_does_not_activate_store_analytics_policy(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Сколько?", limit=5)
    brief = build_agent_brief(store, "Сколько?", limit=5)

    assert result["has_knowledge"] is False
    assert brief["route"]["domain"] is None
    assert brief["route"]["open_first"] is None


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
        "Покажи ошибки выгрузки за 24 часа",
        "Покажи ошибки выгрузки за 7 дней",
        "Покажи товар по артикулу",
        "Найди заказ магазина по номеру",
        "Покажи заявки без исполнителя",
        "Покажи активных поставщиков",
        "Покажи партии в ячейке",
        "Покажи незавершенные приходы",
        "Покажи незавершенные отгрузки",
        "Покажи проблемные объявления маркетплейса",
        "Покажи состояние маркетплейсов",
        "Найди варианты поставки ROSSKO",
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
        "замени внутренний комментарий заявки",
        "очисти внутренний комментарий заявки",
        "добавь комментарий в заявку на проценку",
        "добавь заметку в заявку на проценку",
        "добавь запись в историю заявки на проценку",
        "подготовь черновики для заявки на проценку",
        "замени приватные черновики предложений",
        "очисти приватные черновики предложений",
        "измени место хранения партии",
        "переложи партию в ячейку",
        "переведи заказ магазина в READY",
        "отметь собранный заказ готовым",
    ]

    for phrase in phrases:
        route = find_command_route(phrase)
        assert route is not None, phrase
        assert route["workflow_id"] == "store_management_workflow", phrase


def test_full_store_owner_parity_phrases_route_to_management_workflow():
    for phrase in [
        "создай товар в магазине",
        "измени цену товара",
        "прими товар на склад",
        "оформи возврат магазина",
        "опубликуй предложение магазина",
        "измени настройки магазина",
    ]:
        route = find_command_route(phrase)
        assert route is not None, phrase
        assert route["workflow_id"] == "store_management_workflow", phrase
        assert "store_owner_api" in route["write_domains"], phrase


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
