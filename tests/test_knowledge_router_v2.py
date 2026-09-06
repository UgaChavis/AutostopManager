from __future__ import annotations

import pytest

import autostop_manager.knowledge_base as knowledge_base
from autostop_manager.context import build_agent_brief
from autostop_manager.knowledge_base import plan_command_routes, probe_knowledge_base


def _routes(query: str, *, intent: str | None = None) -> list[dict]:
    return plan_command_routes(query, intent=intent)


def _ids(query: str, *, intent: str | None = None) -> list[str]:
    return [route["command_id"] for route in _routes(query, intent=intent)]


def test_routes_are_broad_effect_free_suggestions():
    queries = (
        "Найди компанию в CRM, подготовь счёт и отправь по Gmail",
        "Обработай заявку на проценку и ответь клиенту в Telegram",
        "Перезапусти SSHD на удалённом сервере",
    )

    for query in queries:
        routes = _routes(query)
        assert 1 <= len(routes) <= 3
        assert all(route["effects"] == [] for route in routes)
        assert all(route["dependencies"] == [] for route in routes)
        assert all(route["phase"] == 0 for route in routes)


def test_composed_request_offers_capabilities_without_a_sequence():
    routes = _routes("Найди компанию Horizon в CRM, выставь счёт и отправь на эту почту")

    assert {route["command_id"] for route in routes} == {"crm_operations", "documents_and_mail"}
    assert routes[0]["score"] >= routes[1]["score"]


def test_service_case_uses_one_broad_route():
    routes = _routes("Найди оригинальный номер детали по VIN")

    assert _ids("Найди оригинальный номер детали по VIN") == ["service_case"]
    assert routes[0]["knowledge_domains"] == ["service_case"]


def test_store_quote_and_telegram_can_be_combined_without_authorizing_actions():
    routes = _routes("Обработай новую заявку на проценку и ответь клиенту в Telegram")

    assert {route["command_id"] for route in routes} == {"store_quote", "telegram_operations"}
    assert all(route["effects"] == [] for route in routes)


@pytest.mark.parametrize(
    "query",
    [
        "Ответь клиенту по Gmail",
        "Ответь клиенту в CRM",
        "Свяжись с клиентом по телефону",
    ],
)
def test_customer_dialogue_does_not_assume_telegram_channel(query):
    assert "telegram_operations" not in _ids(query)


@pytest.mark.parametrize(
    "query",
    [
        "Уточни у клиента по заявке магазина",
        "Ответь клиенту по заявке магазина",
        "Свяжись с клиентом по предложению магазина",
    ],
)
def test_store_customer_dialogue_can_suggest_telegram_without_naming_the_channel(query):
    routes = _routes(query)

    assert "telegram_operations" in {route["command_id"] for route in routes}
    assert all(route["effects"] == [] for route in routes)


@pytest.mark.parametrize(
    "query",
    [
        "Ответь клиенту в WhatsApp",
        "Ответь клиенту по SMS",
        "Ответь клиенту через сайт",
    ],
)
def test_customer_dialogue_respects_an_explicit_non_telegram_channel(query):
    assert "telegram_operations" not in _ids(query)


@pytest.mark.parametrize(
    "query",
    [
        "Создай коммерческое предложение для клиента",
        "Опубликуй предложение по вакансии",
    ],
)
def test_generic_proposals_do_not_assume_store(query):
    assert "store_quote" not in _ids(query)


@pytest.mark.parametrize(
    "query",
    [
        "Клиент подтвердил опубликованное предложение, создай заказ",
        "Клиент согласовал актуальное опубликованное предложение по проценке, оформи выбранный заказ",
    ],
)
def test_published_store_choice_can_suggest_quote_workflow_without_authorizing_an_order(query):
    routes = _routes(query)

    assert "store_quote" in {route["command_id"] for route in routes}
    assert all(route["effects"] == [] for route in routes)


def test_supplier_offer_does_not_become_a_store_customer_quote():
    assert "store_quote" not in _ids("Проверь опубликованное предложение поставщика")


def test_technical_instruction_stays_in_service_case():
    assert _ids("Проверь инструкцию по ремонту двигателя") == ["service_case"]


def test_manager_name_does_not_override_a_concrete_service_request():
    assert _ids("В AutoStopManager найди OEM по VIN") == ["service_case"]


@pytest.mark.parametrize(
    ("intent", "command_id"),
    [
        ("crm_finance_operation", "crm_operations"),
        ("store_quote_intake", "store_quote"),
        ("store_quote_conductor_draft", "store_quote"),
        ("store_customer_response_publish", "store_quote"),
        ("store_quote_order_confirm", "store_quote"),
        ("telegram_response_draft", "telegram_operations"),
        ("telegram_authorization", "telegram_operations"),
    ],
)
def test_legacy_intents_resolve_to_broad_suggestions(intent, command_id):
    routes = _routes("unrelated words", intent=intent)

    assert [route["command_id"] for route in routes] == [command_id]
    assert routes[0]["score"] == 1000
    assert routes[0]["effects"] == []


def test_current_documentation_request_does_not_become_a_crm_audit():
    query = "Проанализируй главные инструкции проекта Автостоп Менеджер и предложи, как сделать агента свободнее"

    assert _ids(query) == ["manager_project"]


@pytest.mark.parametrize(
    "query",
    [
        "Проанализируй документацию Автостоп Менеджер про Telegram и Store",
        "Оптимизируй инструкции и маршруты агента для магазина и телеграма",
        "Добавь в документацию пример: «Ответь клиенту по заявке магазина»",
    ],
)
def test_project_documentation_does_not_open_business_workflows(query):
    assert _ids(query) == ["manager_project"]


def test_candidate_in_crm_does_not_become_a_telegram_customer():
    routes = _ids("Ответь клиенту по заявке кандидата в CRM")

    assert "crm_operations" in routes
    assert "telegram_operations" not in routes


def test_scope_exclusion_is_respected():
    assert all(
        "store_management" not in route["knowledge_domains"]
        for route in _routes("Покажи активные заказы магазина, но без Store — магазин не трогать")
    )


def test_remote_access_and_pad_remain_separate_capabilities():
    assert _ids("Проверь подключение к удалённому серверу по SSH") == ["remote_and_release"]
    assert _ids("Подготовь удалённую диагностику Launch PAD VII") == ["remote_diagnostics"]


def test_command_registry_and_knowledge_map_are_independent(tmp_path, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(knowledge_base, "KNOWLEDGE_MAP_PATH", tmp_path / "missing-map.json")
        assert _ids("Приберись в CRM") == ["crm_operations"]

    with monkeypatch.context() as patched:
        patched.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
        result = probe_knowledge_base(None, "BMW F15 N63", limit=5)
        assert result["best_domain"] == "service_case"
        assert result["routes"]


def test_unmatched_question_stays_bounded_exploration():
    brief = build_agent_brief(None, "Как выставить ГРМ на Mercedes M274?")

    assert brief["route"]["selection_mode"] == "explore"
    assert brief["route"]["steps"] == []
    assert brief["route"]["candidates"]
    assert all(candidate["confidence"] < 0.5 for candidate in brief["route"]["candidates"])


@pytest.mark.parametrize(
    "query",
    [
        "Напиши текст ответа клиенту в Telegram",
        "Клиент выбрал предложение",
        "Клиент подтвердил опубликованное предложение, создай заказ",
        "Обсудим команду «Опубликуй предложение», пока ничего не меняй",
    ],
)
def test_drafts_choices_and_quoted_commands_never_gain_permissions(query):
    assert all(route["effects"] == [] for route in _routes(query))
