from __future__ import annotations

import pytest

import autostop_manager.knowledge_base as knowledge_base
from autostop_manager.context import build_agent_brief
from autostop_manager.knowledge_base import plan_command_routes, probe_knowledge_base


def _routes(query: str, *, intent: str | None = None) -> list[dict]:
    return plan_command_routes(query, intent=intent)


def _workflows(query: str, *, intent: str | None = None) -> list[str]:
    return [route["workflow_id"] for route in _routes(query, intent=intent)]


def test_representative_composed_business_workflow_is_phase_ordered():
    query = "Найди компанию Horizon, выставь счёт и отправь на эту почту"

    assert _workflows(query) == [
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    ]
    brief = build_agent_brief(None, query)
    assert [step["phase"] for step in brief["route"]["steps"]] == [10, 20, 30]
    assert brief["route"]["write_domains"] == ["crm"]
    assert brief["route"]["external_connectors"] == ["gmail"]


def test_service_routes_share_the_current_service_case_domain():
    routes = _routes("Найди оригинальный номер детали по VIN")

    assert [route["workflow_id"] for route in routes] == [
        "vehicle_identity_decode",
        "parts_oem_lookup",
    ]
    assert all(route["knowledge_domains"] == ["service_case"] for route in routes)


@pytest.mark.parametrize(
    ("query", "command_id", "effects"),
    [
        ("Бери в работу заявку на проценку", "store_quote_intake", []),
        ("Подготовь позиции в заявке на проценку", "store_quote_draft", ["store_write"]),
        (
            "Опубликуй предложения по заявке на проценку",
            "store_customer_response_publish",
            ["store_write"],
        ),
    ],
)
def test_store_quote_stages_keep_distinct_effect_boundaries(query, command_id, effects):
    routes = _routes(query)

    assert [route["command_id"] for route in routes] == [command_id]
    route = routes[0]
    assert route["workflow_id"] == "store_quote_conductor"
    assert route["effects"] == effects
    assert route["knowledge_domains"] == ["store_management"]


def test_store_and_telegram_compose_without_broadening_store_publish():
    composed = _routes("Обработай новую заявку на проценку и ответь клиенту в Telegram")
    assert [route["command_id"] for route in composed] == [
        "store_quote_intake",
        "telegram_owner_operations",
    ]
    assert composed[0]["effects"] == []
    assert composed[1]["effects"] == ["external_send"]

    telegram_only = _routes("Ответь клиенту в Telegram по заявке на проценку, публикацию Store не делай")
    assert [route["command_id"] for route in telegram_only] == ["telegram_owner_operations"]
    assert all("store_write" not in route["effects"] for route in telegram_only)

    opted_out = _routes("Обработай новую заявку на проценку, но в Telegram не отвечай")
    assert [route["command_id"] for route in opted_out] == ["store_quote_intake"]
    assert all("external_send" not in route["effects"] for route in opted_out)
    assert all("telegram_operations" not in route["knowledge_domains"] for route in opted_out)


def test_natural_store_handoff_and_read_only_opt_out_keep_the_right_telegram_route():
    handoff = _routes("Уточни у клиента по заявке магазина")
    assert {route["intent"] for route in handoff} == {"store_read", "telegram_operations"}
    assert [route["effects"] for route in handoff] == [[], ["external_send"]]

    read_only = _routes("Прочитай последние сообщения в Telegram, ничего не отправляй")
    assert [route["command_id"] for route in read_only] == ["telegram_read_operations"]
    assert read_only[0]["effects"] == []


@pytest.mark.parametrize(
    ("query", "send"),
    [
        ("В рабочем телеграме клиент прислал WBAFR9C50BC123456 и спросил сколько выйдет", False),
        ("Клиент прислал артикул 1K0615301AA в телеграм", False),
        ("В рабочем Telegram фото детали", False),
        ("Клиент прислал VIN WBAFR9C50BC123456, ответь ему", True),
        ("Клиент в Telegram написал: беру Bosch", False),
        ("WBAFR9C50BC123456", False),
    ],
)
def test_terse_telegram_parts_context_invites_research_without_implicit_mutation(query, send):
    routes = _routes(query)
    intents = {route["intent"] for route in routes}

    assert {"store_read", "telegram_read"} <= intents
    assert all("store_write" not in route["effects"] for route in routes)
    assert any("external_send" in route["effects"] for route in routes) is send


@pytest.mark.parametrize("query", ["Диагностика VIN WBAFR9C50BC123456", "Найди этот VIN в CRM WBAFR9C50BC123456"])
def test_vin_in_another_explicit_context_does_not_become_store_work(query):
    assert all(route["knowledge_domains"] != ["store_management"] for route in _routes(query))


def test_cleanup_request_stays_out_of_finance_business_documents_and_crm_audit():
    query = (
        "Сократи кодовую базу и очисти дублирующие инструкции и документацию AutoStopManager; с минимальными тестами."
    )
    routes = _routes(query)

    assert [route["command_id"] for route in routes] == [
        "ecosystem_capability_parity",
        "manager_documentation_hygiene",
    ]
    forbidden = {"business_document_workflow", "crm_agent_integration_audit", "crm_finance_operation"}
    assert forbidden.isdisjoint(route["command_id"] for route in routes)
    assert all("finance" not in route["effects"] for route in routes)


@pytest.mark.parametrize(
    "query",
    [
        "Обсудим команду «Опубликуй предложения по заявке на проценку», пока ничего не меняй",
        "Проверь маршрут для фразы «обработай заявку на проценку»",
        "Покажи черновик ответа клиенту по заявке на проценку",
    ],
)
def test_deferred_or_quoted_commands_never_authorize_effects(query):
    assert all(not route["effects"] for route in _routes(query))


def test_explicit_intent_is_deterministic_and_scope_opt_out_is_respected():
    routes = _routes("письмо про оплату и документ", intent="crm_finance_operation")
    assert [route["workflow_id"] for route in routes] == ["crm_finance_operation"]
    assert routes[0]["score"] == 1000

    assert _routes("Покажи активные заказы магазина, но без Store — магазин не трогать") == []
    assert _routes("Покажи активные заказы магазина, но без Store — магазин не трогать", intent="store_read") == []


def test_remote_read_change_and_pad_routes_remain_separate():
    read = _routes("Проверь подключение к удалённому серверу по SSH")
    assert [route["workflow_id"] for route in read] == ["remote_codex_access"]
    assert read[0]["effects"] == []

    change = _routes("Перезапусти SSHD на удалённом сервере")
    assert [route["workflow_id"] for route in change] == ["remote_codex_access_change"]
    assert change[0]["effects"] == ["destructive"]
    assert change[0]["dependencies"] == ["remote_codex_access"]

    pad = _routes("Подготовь удалённую диагностику Launch PAD VII")
    assert [route["workflow_id"] for route in pad] == ["remote_diagnostics_pad_vii"]
    assert pad[0]["effects"] == ["remote_diagnostics"]


def test_command_registry_and_knowledge_map_are_independent(tmp_path, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(knowledge_base, "KNOWLEDGE_MAP_PATH", tmp_path / "missing-map.json")
        assert _workflows("Приберись") == ["board_cleanup_autopilot"]

    with monkeypatch.context() as patched:
        patched.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
        result = probe_knowledge_base(None, "BMW F15 N63", limit=5)
        assert result["best_domain"] == "service_case"
        assert result["routes"]


def test_unmatched_technical_question_stays_bounded_exploration():
    brief = build_agent_brief(None, "Как выставить ГРМ на Mercedes M274?")

    assert brief["route"]["selection_mode"] == "explore"
    assert brief["route"]["steps"] == []
    assert brief["route"]["candidates"][0]["confidence"] < 0.5


@pytest.mark.parametrize(
    "query",
    [
        "Подготовь ответ по заявке на проценку в CRM",
        "Добавь позицию в заявку на проценку в CRM",
        "Опубликуй предложение по заявке на проценку в CRM",
    ],
)
def test_crm_scoped_quote_language_never_authorizes_store_write(query):
    assert all("store_write" not in route["effects"] for route in _routes(query))


def test_ambiguous_requests_do_not_gain_store_or_external_effects():
    for query in ("Обработай новый запрос", "Нам пришел заказ, обработай его", "Ответь клиенту по заявке кандидата"):
        routes = _routes(query)
        assert all("store_write" not in route["effects"] for route in routes)
        assert all("external_send" not in route["effects"] for route in routes)
