from __future__ import annotations

import pytest

from autostop_manager.knowledge_base import find_command_route, probe_knowledge_base, sync_knowledge_base
from autostop_manager.routing import classify_query
from autostop_manager.storage import ManagerMemoryStore


@pytest.fixture(scope="module")
def routed_store(tmp_path_factory: pytest.TempPathFactory) -> ManagerMemoryStore:
    store = ManagerMemoryStore(tmp_path_factory.mktemp("semantic-router") / "memory.sqlite3")
    result = sync_knowledge_base(store)
    assert result["ok"] is True
    return store


@pytest.mark.parametrize(
    "query",
    [
        "Проведи полный обзор архитектуры проекта AutoStop Manager",
        "Полностью обследуй проект: маршрутизатор, MCP, SQLite, документацию и тесты",
        "Изучи весь репозиторий, исправь техдолг, проверь GitHub и разверни на сервере",
    ],
)
def test_broad_project_requests_use_general_maintenance_route(routed_store, query):
    result = probe_knowledge_base(routed_store, query, limit=5)

    assert result["has_knowledge"] is True
    assert result["best_domain"] == "project_maintenance"
    assert result["open_first"] == "docs/agent/architecture.md"
    assert result["semantics"]["broad_project_request"] is True
    assert result["best_domain"] not in {"parts_sourcing", "deployment"}


def test_exact_owner_alias_remains_authoritative(routed_store):
    route = find_command_route("Приберись")
    result = probe_knowledge_base(routed_store, "Приберись", limit=5)

    assert route is not None
    assert route["match_kind"] == "exact_alias"
    assert route["confidence"] == 0.99
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["has_knowledge"] is True


def test_vin_crm_and_parts_overlap_uses_specific_writeback_route(routed_store):
    result = probe_knowledge_base(
        routed_store,
        "В карточке CRM по VIN найди OEM каталожный номер фильтра, аналоги и запиши результат",
        limit=5,
    )

    assert result["best_domain"] == "crm_vin_oem_parts_lookup"
    assert result["has_knowledge"] is True
    assert {"crm", "parts", "vehicle_identity"}.issubset(result["semantics"]["objects"])
    assert result["semantics"]["access_mode"] == "mixed"


def test_raw_vin_without_vin_keyword_routes_to_vehicle_identity(routed_store):
    result = probe_knowledge_base(
        routed_store,
        "WDB4633501X334217 что за машина",
        limit=5,
    )

    assert result["best_domain"] == "vehicle_identity_and_oem"
    assert result["has_knowledge"] is True


def test_parts_without_vehicle_identity_stays_in_sourcing(routed_store):
    result = probe_knowledge_base(
        routed_store,
        "В заказ-наряде оригинальный номер и заменитель, найди закупочную цену",
        limit=5,
    )

    assert result["best_domain"] == "parts_sourcing"
    assert result["has_knowledge"] is True


def test_gmail_memory_server_and_docs_have_distinct_semantic_routes(routed_store):
    cases = {
        "Проверь Gmail, ярлыки, вложения и черновики без записи": "gmail_operations",
        "Проверь SQLite-память, миграции и восстановление после остановки": "startup_and_identity",
        "Проверь состояние сервера, Docker и контейнеров": "deployment",
        "Обнови документацию, playbook и исправь битые ссылки": "knowledge_intake",
    }

    for query, expected_domain in cases.items():
        result = probe_knowledge_base(routed_store, query, limit=5)
        assert result["best_domain"] == expected_domain, query
        assert result["has_knowledge"] is True, query


def test_dangerous_mixed_source_write_is_not_forced_into_one_route(routed_store):
    result = probe_knowledge_base(
        routed_store,
        "Удали карточку CRM и архивируй письма Gmail",
        limit=5,
    )

    assert result["has_knowledge"] is False
    assert result["route_status"] == "ambiguous"
    assert result["semantics"]["risk_level"] == "high"
    assert "gmail_operations" in result["ambiguous_candidates"]
    assert any(domain in result["ambiguous_candidates"] for domain in {"service_management", "board_cleanup_autopilot"})


def test_no_keywords_uses_safe_general_fallback_without_claiming_knowledge(routed_store):
    result = probe_knowledge_base(routed_store, "Помоги разобраться", limit=5)

    assert result["best_domain"] == "project_maintenance"
    assert result["has_knowledge"] is False
    assert result["route_status"] == "fallback"
    assert result["confidence"] < 0.45


def test_one_common_word_cannot_saturate_confidence(routed_store):
    result = probe_knowledge_base(routed_store, "сервер", limit=5)

    assert result["has_knowledge"] is False
    assert result["confidence"] <= 0.4


def test_semantics_exposes_action_source_output_and_risk_dimensions():
    semantics = classify_query(
        "Проведи аудит проекта, исправь код, создай отчёт, отправь в GitHub и разверни на сервере"
    ).as_dict()

    assert {"audit", "fix", "publish", "deploy"}.issubset(semantics["actions"])
    assert {"local_repo", "server"}.issubset(semantics["sources"])
    assert {"report", "code_change", "pull_request", "deployment"}.issubset(semantics["outputs"])
    assert semantics["access_mode"] == "mixed"
    assert semantics["risk_level"] == "high"


def test_regression_broad_router_review_does_not_route_to_parts(routed_store):
    result = probe_knowledge_base(
        routed_store,
        "Проведи широкий обзор AutoStop Manager и исправь маршрутизацию запросов, тесты и документацию",
        limit=5,
    )

    assert result["best_domain"] == "project_maintenance"
    assert result["best_domain"] != "parts_sourcing"
    assert result["route_margin"] >= 8
