from __future__ import annotations

import json

import pytest

import autostop_manager.knowledge_base as knowledge_base
from autostop_manager.context import build_agent_brief
from autostop_manager.knowledge_base import (
    plan_command_routes,
    probe_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def _workflows(query: str, *, intent: str | None = None) -> list[str]:
    return [route["workflow_id"] for route in plan_command_routes(query, intent=intent)]


def _queries(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", "|").split("|") if item.strip()]


def _only_route(query: str) -> dict:
    routes = plan_command_routes(query)
    assert len(routes) == 1
    return routes[0]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Найди компанию Горизонт и покажи её данные", ["crm_record_workflow"]),
        ("В Audi Q7 поставь клиентом компанию Horizon", ["crm_record_workflow"]),
        ("Есть ли у компании Horizon почта?", ["crm_record_workflow"]),
        ("Измени клиента в заказ-наряде", ["crm_record_workflow"]),
        ("Выставь счёт из CRM", ["business_document_workflow"]),
        (
            "Выставь счёт и отправь на эту почту",
            ["business_document_workflow", "crm_gmail_workflow"],
        ),
        (
            "Найди компанию Horizon, назначь её клиентом Audi Q7, проверь email, выставь счёт из CRM и отправь его на эту почту",
            ["crm_record_workflow", "business_document_workflow", "crm_gmail_workflow"],
        ),
        (
            "В карточке CRM по VIN найди OEM фильтра и запиши в карточку",
            ["crm_vin_oem_parts_lookup", "crm_record_workflow"],
        ),
        ("Приберись", ["board_cleanup_autopilot"]),
        ("Устрани техдолг в проекте AutoStopManager", ["ecosystem_capability_parity"]),
        (
            "Сократи код и почисти документацию проекта Автостоп Менеджер",
            ["ecosystem_capability_parity", "manager_documentation_hygiene"],
        ),
    ],
)
def test_command_route_regression_table(query, expected):
    assert _workflows(query) == expected


_STORE = "store_management_workflow"
_DRAFT = ("store_write", "finance", "destructive")
_QUOTE_CONDUCTOR_DRAFT = ("store_write",)
_QUOTE_CONDUCTOR_PUBLISH = ("store_write", "external_send")
_EXACT_ROUTE_GROUPS = {
    (
        "store_read_workflow",
        "store_read_workflow",
        (),
    ): "Покажи состояние склада|Получай полную информацию и управляй нашим сайтом автозапчастей|Покажи все каталоги запасных частей и наличие деталей|Посмотри новый запрос на проценку|Посмотри новую заявку на проценку|Разбери новую заявку на проценку|Заказ магазина в READY?|Какой заказ магазина в READY?|Проверь, переведена ли заявка в ждёт согласования?|Заявка уже переведена в ждёт согласования?|Покажи, переведена ли заявка в ждет согласования|Почему заявка переведена в ждёт согласования?|Покажи черновик ответа клиенту по заявке на проценку",
    (
        "store_quote_draft",
        "store_quote_conductor",
        _QUOTE_CONDUCTOR_DRAFT,
    ): "Подготовь черновик ответа клиенту по заявке магазина|Подготовь ответ клиенту по заявке магазина|Подготовь ответ клиенту по заявке на проценку|Заполни позиции в заявке на проценку|Добавь предложение в запрос магазина|Добавь предложение в заявку на проценку|Добавь позицию в заявку на проценку|Измени предложение в заявке на проценку|Измени позицию в заявке на проценку|Обнови стоимость в заявке на проценку|Обнови стоимость позиции в заявке на проценку|Обнови срок в заявке на проценку|Обнови срок позиции в заявке на проценку|Добавь комментарий к предложению в заявке на проценку|Добавь комментарий к ответу по заявке на проценку",
    (
        "store_quote_intake",
        "store_quote_conductor",
        (),
    ): "Бери в работу заявку на проценку|Возьми в работу заявку на проценку|Занимайся заявкой на проценку|Обработай новую заявку магазина|Обработай новую заявку на проценку",
    (
        "store_order_intake",
        "store_management_workflow",
        (),
    ): "Бери в работу заказ магазина|Возьми в работу заказ магазина|Занимайся заказом магазина|Обработай новый заказ магазина|Обработай заказ магазина",
    ("store_management_workflow", _STORE, ("store_write",)): "Добавь комментарий в заявку на проценку",
    (
        "store_customer_response_publish",
        "store_quote_conductor",
        _QUOTE_CONDUCTOR_PUBLISH,
    ): "Переведи заявку в ждёт согласования|Ответь клиенту по заявке магазина|Ответь клиенту по заявке на проценку|Заполни позиции и переведи заявку в ждёт согласования|Опубликуй предложения по заявке на проценку|Опубликуй предложения в заявке на проценку|Опубликуй предложения заявки на проценку|Опубликуй проценку по заявке магазина",
    ("store_price_management", _STORE, _DRAFT): "Измени цену товара|Измени цену товара в магазине",
    ("store_product_create", _STORE, _DRAFT): "Создай товар в магазине",
    ("store_order_ready", _STORE, ("store_write", "external_send", "destructive")): "Переведи заказ магазина в ready",
    (
        "telegram_connector_health",
        "telegram_connector_health",
        (),
    ): "Проверь Telegram|Проверь подключение telegram_bridge|Проверь авторизацию telegram_bridge",
    ("telegram_owner_operations", "telegram_owner_operations", ("external_send",)): "Напиши в Телеграм администратору",
    (
        "telegram_authorization",
        "telegram_owner_operations",
        ("account_auth",),
    ): "Авторизуй Telegram|Авторизуй telegram_bridge",
    (
        "telegram_read_operations",
        "telegram_owner_operations",
        (),
    ): "Найди контакт в Telegram|Прочитай Telegram|Найди контакт через telegram_bridge|Прочитай telegram_bridge|Прочитай рабочий Телеграм|Посмотри сообщения в рабочем Телеграме|Скачай голосовое в рабочем Телеграме|Расшифруй аудио в Telegram|Послушай последнее голосовое в рабочем Телеграме",
}


@pytest.mark.parametrize(("expected", "query_text"), _EXACT_ROUTE_GROUPS.items())
def test_store_and_telegram_exact_route_matrix(expected, query_text):
    command_id, workflow_id, effects = expected
    for query in _queries(query_text):
        route = _only_route(query)
        assert route["command_id"] == command_id
        assert route["workflow_id"] == workflow_id
        assert route["effects"] == list(effects)


@pytest.mark.parametrize(
    "query",
    _queries(
        "Выбери предложение в заявке на проценку|Выбери offer в запросе магазина|Выбери legacy предложение в заявке на проценку"
    ),
)
def test_legacy_quote_offer_selection_is_never_a_public_route(query):
    assert plan_command_routes(query) == []


_COMPOSED_ROUTE_GROUPS = {
    (
        "store_quote_intake",
        "telegram_owner_operations",
    ): "Посмотри новый запрос на проценку, обработай его и ответь клиенту в рабочем Телеграме|Посмотри новый запрос на проценку, обработай его и ответь в Телеграмм клиенту",
    (
        "store_quote_intake",
    ): "Обработай новую заявку на проценку, но в Telegram не отвечай|Обработай новую заявку на проценку без Telegram",
    ("telegram_owner_operations",): "Ответь клиенту в Telegram по заявке на проценку, публикацию Store не делай",
}


@pytest.mark.parametrize(("expected", "query_text"), _COMPOSED_ROUTE_GROUPS.items())
def test_store_and_work_telegram_composition_respects_partial_opt_outs(expected, query_text):
    for query in _queries(query_text):
        routes = plan_command_routes(query)
        assert tuple(route["command_id"] for route in routes) == expected
        if expected[0] == "store_quote_intake":
            assert routes[0]["effects"] == []
            assert routes[0]["knowledge_domains"] == ["store_management", "vehicle_identity_and_oem"]


@pytest.mark.parametrize(
    "query",
    _queries(
        "Обработай новый запрос|Обработай новый запрос на ремонт|Обработай новую заявку кандидата|Обработай новый запрос поставщика|Посмотри новую заявку кандидата|Разбери новую заявку кандидата|Ответь клиенту по заявке кандидата|Посмотри новый запрос в почте|Обработай заявку поставщика в магазине|Обработай запрос на возврат в магазине|Обработай запрос на закупку в магазине|Переведи запрос магазина в работу|Посмотри новый запрос|Прочитай новый запрос|Что за новый запрос"
    ),
)
def test_ambiguous_new_request_never_routes_effectful_store(query):
    routes = plan_command_routes(query)

    assert all(route["command_id"] not in {"store_customer_response_publish", "store_quote_intake"} for route in routes)


@pytest.mark.parametrize("query", ("Бери в работу новый заказ", "Нам пришел заказ, обработай его"))
def test_ambiguous_order_never_routes_store_intake(query):
    assert all(route["command_id"] != "store_order_intake" for route in plan_command_routes(query))


def test_email_lookup_does_not_select_external_send():
    routes = plan_command_routes("Проверь, есть ли у компании Horizon email")

    assert [route["workflow_id"] for route in routes] == ["crm_record_workflow"]
    assert all("external_send" not in route["effects"] for route in routes)


def test_repair_order_client_change_never_becomes_parts_sourcing():
    route = plan_command_routes("Обнови клиента в заказ-наряде")[0]

    assert route is not None
    assert route["workflow_id"] == "crm_record_workflow"
    assert "parts_sourcing" not in route["knowledge_domains"]


def test_company_vehicle_and_address_values_are_not_signals():
    for value in ("Horizon", "Audi Q7", "7701234567", "client@example.com"):
        assert plan_command_routes(value) == []


def test_real_parts_and_oem_requests_remain_distinct():
    generic = _workflows("Найди запчасти по VIN для BMW N63")
    oem = _workflows("Найди оригинальный номер детали по VIN")
    crosses = _workflows("Проверь кроссы и аналоги по номеру детали")
    writeback = _workflows("В карточке CRM по VIN найди OEM фильтра и запиши в карточку")

    assert generic == ["vehicle_identity_decode", "parts_oem_lookup"]
    assert oem == ["vehicle_identity_decode", "parts_oem_lookup"]
    assert crosses == ["parts_oem_lookup"]
    assert writeback[0] == "crm_vin_oem_parts_lookup"


def test_explicit_intent_is_deterministic():
    route = plan_command_routes("письмо про оплату и документ", intent="crm_finance_operation")[0]

    assert route is not None
    assert route["workflow_id"] == "crm_finance_operation"
    assert route["score"] == 1000
    assert _workflows("письмо про оплату и документ", intent="crm_finance_operation") == ["crm_finance_operation"]


def test_explicit_store_opt_out_removes_store_routes():
    query = "Покажи активные заказы магазина, но без Store — магазин не трогать"

    assert plan_command_routes(query) == []
    assert plan_command_routes(query, intent="store_read") == []


def test_project_maintenance_respects_store_opt_out():
    routes = plan_command_routes("Устрани техдолг Автостоп Менеджера без работы со Store")

    assert [route["workflow_id"] for route in routes] == ["ecosystem_capability_parity"]


def test_crm_failure_is_not_treated_as_negative_scope():
    assert _workflows("CRM не работает") == ["crm_agent_integration_audit"]


@pytest.mark.parametrize(
    "query",
    _queries(
        "Проверь работоспособность AutoStopManager|Коротко протестируй Автостоп Менеджер|Сделай smoke-test AutoStopManager|Проверь интеграции AutoStopManager"
    ),
)
def test_manager_health_routes_without_requiring_mcp_keyword(query):
    assert _workflows(query) == ["crm_agent_integration_audit"]


@pytest.mark.parametrize(
    "query",
    _queries(
        "Короткий read-only smoke-test AutoStopManager: проверить Gmail, Telegram, CRM, команды и MCP-серверы; без Store|Протестируй Автостоп Менеджер: работают ли Gmail, Телеграм, CRM, все команды и MCP сервера|Коротко протестируй Автостоп Менеджера: Gmail, Telegram, CRM, чтение карточек, все команды и MCP-серверы подключены и работоспособны"
    ),
)
def test_manager_smoke_routes_to_integration_audit_without_remote_access(query):
    routes = plan_command_routes(query)

    assert [route["workflow_id"] for route in routes] == [
        "crm_agent_integration_audit",
        "telegram_connector_health",
    ]
    assert all(not route["effects"] for route in routes)


@pytest.mark.parametrize(
    "query",
    _queries(
        "Только не отправь это клиенту в рабочем Телеграме|Подготовлен ли черновик ответа клиенту по заявке магазина?|Как заполнить позиции в заявке на проценку?|Покажи заполненные позиции в заявке на проценку|Создан ли товар в магазине?|Можно ли создать товар в магазине?|Как создать товар в магазине?|Как изменить цену товара в магазине?|Как обработать заявку на проценку?|Покажи, как обработать заявку на проценку|Обсудим команду «Обработай заявку на проценку», пока ничего не меняй|Расскажи, как выполнить «Опубликуй предложения по заявке», сейчас ничего не делай|Потом создай товар в магазине, но сейчас пока ничего не делай|Давай обсудим, как ты заполни позиции; ничего не меняй|Что делает кнопка «Опубликуй предложения по заявке на проценку»?|Объясни команду «Измени цену товара в магазине»|Что значит «Обработай новую заявку магазина»?|Если написать «Ответь в Telegram клиенту», сообщение сразу уйдёт?|Как выполнить команду «Опубликуй предложения по заявке на проценку»?|Напиши инструкцию для команды «отправь в Telegram»|Покажи пример: заполни позиции в заявке на проценку|Проверь маршрут для фразы «обработай заявку на проценку»|Обработай новую заявку на проценку, но не отвечай клиенту|Посмотри заявку на проценку, ничего не записывай"
    ),
)
def test_meta_questions_deferrals_and_negations_never_authorize_effects(query):
    assert all(not route["effects"] for route in plan_command_routes(query))


@pytest.mark.parametrize(
    "query",
    _queries(
        "Измени цену запчасти в заказ-наряде CRM|Подготовь ответ по заявке на проценку в CRM|Добавь позицию в заявку на проценку в CRM|Добавь комментарий в заявку на проценку CRM|Опубликуй предложение по заявке на проценку в CRM"
    ),
)
def test_crm_scoped_requests_never_authorize_store_writes(query):
    assert all("store_write" not in route["effects"] for route in plan_command_routes(query))


def test_no_live_review_request_does_not_route_to_telegram_runtime():
    routes = plan_command_routes("Проверь diff инструкций Store и Telegram без live-вызовов")

    assert all("telegram_operations" not in route["knowledge_domains"] for route in routes)


def test_explicit_telegram_send_composes_with_integration_audit():
    assert _workflows("Проверь MCP и отправь отчёт в Telegram") == [
        "crm_agent_integration_audit",
        "telegram_owner_operations",
    ]


def test_explicit_remote_server_still_uses_remote_access_route():
    assert _workflows("Проверь подключение к удалённому серверу по SSH") == ["remote_codex_access"]


@pytest.mark.parametrize(
    "query",
    _queries("Перезапусти SSHD на удалённом сервере|Запусти bootstrap home-pc"),
)
def test_explicit_remote_change_uses_destructive_route(query):
    routes = plan_command_routes(query)

    assert [route["workflow_id"] for route in routes] == ["remote_codex_access_change"]
    assert routes[0]["effects"] == ["destructive"]
    assert routes[0]["dependencies"] == ["remote_codex_access"]


@pytest.mark.parametrize(
    "query",
    _queries(
        "Проверь свежесть и восстановимость резервных копий AutoStop перед обновлением|Проверь цифровую инфраструктуру AutoStop перед обновлением|Проверь серверную инфраструктуру AutoStop перед обновлением"
    ),
)
def test_release_readiness_audit_is_not_misrouted_to_remote_access(query):
    assert _workflows(query) == []
    result = probe_knowledge_base(None, query)
    assert result["best_domain"] == "deployment"
    assert "remote_codex_access" not in {route["domain"] for route in result["routes"]}


@pytest.mark.parametrize("alias", ["autostop-vps27560", "autostop-vps27560-alt"])
def test_configured_server_aliases_use_remote_access_route(alias):
    assert _workflows(f"Проверь {alias}") == ["remote_codex_access"]


@pytest.mark.parametrize(
    "query",
    _queries("Подготовь удаленную диагностику Launch PAD VII|Начни сессию AutoStop Remote на планшете Launch"),
)
def test_pad_vii_diagnostics_route_is_distinct_from_remote_access(query):
    assert _workflows(query) == ["remote_diagnostics_pad_vii"]


def test_command_routes_are_read_live_without_sync_or_restart(tmp_path, monkeypatch):
    route_path = tmp_path / "command_routes.json"
    monkeypatch.setattr(knowledge_base, "COMMAND_ROUTES_PATH", route_path)

    def write_route(workflow_id: str) -> None:
        route_path.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "workflow_id": workflow_id,
                            "intent": workflow_id,
                            "phase": 10,
                            "priority": 1,
                            "knowledge_domains": ["startup_and_identity"],
                            "effects": [],
                            "signals": {"phrases": ["live route"]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    write_route("first")
    assert _workflows("live route") == ["first"]
    write_route("second")
    assert _workflows("live route") == ["second"]


def test_candidates_use_relevance_while_steps_use_phase(tmp_path, monkeypatch):
    route_path = tmp_path / "command_routes.json"
    monkeypatch.setattr(knowledge_base, "COMMAND_ROUTES_PATH", route_path)
    route_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "workflow_id": "first_step",
                        "phase": 10,
                        "priority": 1,
                        "knowledge_domains": ["startup_and_identity"],
                        "effects": [],
                        "signals": {"all": [["exact"], ["route"]]},
                    },
                    {
                        "workflow_id": "best_match",
                        "phase": 30,
                        "priority": 1,
                        "knowledge_domains": ["startup_and_identity"],
                        "effects": [],
                        "signals": {"phrases": ["exact route"]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    brief = build_agent_brief(ManagerMemoryStore(tmp_path / "memory.sqlite3"), "exact route")

    assert brief["route"]["selected_workflows"] == ["first_step", "best_match"]
    assert [item["workflow_id"] for item in brief["route"]["candidates"]] == [
        "best_match",
        "first_step",
    ]


def test_probe_is_document_only_and_never_returns_command_route(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Приберись", limit=5)

    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["command_route"] is None


def test_manager_smoke_probe_does_not_open_remote_access_docs(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "Короткий smoke-test AutoStopManager: проверить Gmail, Telegram, CRM и MCP-серверы без Store",
        limit=5,
    )

    assert result["best_domain"] == "startup_and_identity"
    assert "remote_codex_access" not in {route["domain"] for route in result["routes"]}


def test_command_registry_and_knowledge_map_are_independent(tmp_path, monkeypatch):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    original_routes = knowledge_base._load_command_routes

    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_MAP_PATH", tmp_path / "missing-map.json")
    assert plan_command_routes("Приберись")[0]["workflow_id"] == "board_cleanup_autopilot"

    monkeypatch.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
    monkeypatch.undo()
    monkeypatch.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
    result = probe_knowledge_base(store, "BMW F15 N63", limit=5)
    assert result["best_domain"] == "automotive_repair"
    monkeypatch.setattr(knowledge_base, "_load_command_routes", original_routes)


def test_agent_brief_without_command_match_is_bounded_exploration(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    brief = build_agent_brief(store, "Как выставить ГРМ на Mercedes M274?", limit=5)

    assert brief["route"]["selection_mode"] == "explore"
    assert brief["route"]["workflow_id"] is None
    assert brief["route"]["steps"] == []
    assert brief["route"]["confidence"] < 0.5
    assert all(candidate["confidence"] < 0.5 for candidate in brief["route"]["candidates"])


def test_full_flow_brief_exposes_ordered_steps_and_effect_policies(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    query = "Найди компанию Horizon, выставь счёт и отправь на эту почту"

    brief = build_agent_brief(store, query, limit=8)

    assert brief["route"]["selected_workflows"] == [
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    ]
    assert [step["phase"] for step in brief["route"]["steps"]] == [10, 20, 30]
    assert brief["route"]["write_domains"] == ["crm"]
    assert brief["route"]["external_connectors"] == ["gmail"]
    assert brief["route"]["steps"][2]["effects"] == ["external_send"]
    assert any("monetary or tax mismatch" in item for item in brief["forbidden_actions"])
