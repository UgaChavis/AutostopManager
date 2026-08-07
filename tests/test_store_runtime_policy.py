from __future__ import annotations

import json
from pathlib import Path

import pytest

import autostop_manager.runtime_policy as runtime_policy
from autostop_manager import context as context_module
from autostop_manager.action_contract import prepare_action_contract
from autostop_manager.agent_gateway import build_agent_bootstrap, list_agent_workflows
from autostop_manager.context import build_agent_brief
from autostop_manager.knowledge_base import (
    STORE_DEPENDENT_DOMAINS,
    find_command_route,
    probe_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


ROOT = Path(__file__).resolve().parents[1]


def _set_store_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str) -> None:
    payload = json.loads((ROOT / "docs" / "agent" / "manager_rules.json").read_text(encoding="utf-8"))
    payload["runtime_policies"]["autostop_store"]["state"] = state
    path = tmp_path / "manager_rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runtime_policy, "MANAGER_RULES_PATH", path)


def _memory(tmp_path: Path) -> ManagerMemoryStore:
    memory = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(memory)
    return memory


def test_canonical_store_policy_is_paused_and_globally_blocks_normal_store_queries(tmp_path: Path) -> None:
    policy = runtime_policy.get_store_access_policy()
    assert policy.state == "paused"
    assert policy.valid is True
    assert policy.paused is True

    memory = _memory(tmp_path)
    query = "Покажи состояние склада и ошибки выгрузки за неделю"
    result = probe_knowledge_base(memory, query, limit=5)
    brief = build_agent_brief(memory, query, limit=5)

    assert find_command_route(query) is None
    assert result["next_action"] == "store_access_paused"
    assert result["best_domain"] is None
    assert result["routes"] == []
    assert brief["route"]["domain"] is None
    assert "store_api" not in brief["memory_sources"]
    assert {"store", "store_analytics"}.isdisjoint(brief["source_boundaries"])
    assert brief["route"]["external_connectors"] == []
    assert brief["route"]["read_entity_selection"] == {}
    assert brief["route"]["operation_selection"] == {}
    assert brief["route"]["selected_operation"] is None
    assert brief["runtime_policies"]["autostop_store"]["effective_paused"] is True

    for paused_query in (
        "сколько посетителей сегодня",
        "полный функциональный паритет CRM и магазина",
    ):
        route = find_command_route(paused_query)
        assert route is None or route["domain"] not in STORE_DEPENDENT_DOMAINS

    workflows = list_agent_workflows(query=query, limit=100)["summary"]["items"]
    assert all(item["domain"] not in STORE_DEPENDENT_DOMAINS for item in workflows)


def test_explicit_reauthorization_config_restores_store_routes_and_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_store_policy(monkeypatch, tmp_path, "enabled")
    memory = _memory(tmp_path)
    query = "Покажи состояние склада"

    route = find_command_route(query)
    result = probe_knowledge_base(memory, query, limit=5)
    brief = build_agent_brief(memory, query, limit=5)

    assert runtime_policy.get_store_access_policy().paused is False
    assert route is not None
    assert route["domain"] == "store_management"
    assert result["best_domain"] == "store_management"
    assert brief["route"]["domain"] == "store_management"
    assert "store" in brief["source_boundaries"]
    assert any("AutoStop App" in item for item in brief["route"]["external_connectors"])
    assert brief["runtime_policies"]["autostop_store"]["effective_paused"] is False
    assert any("agent_bootstrap" in step for step in brief["context_safety"]["recovery"])

    workflows = list_agent_workflows(query=query, limit=100)["summary"]["items"]
    assert any(item["domain"] == "store_management" for item in workflows)


def test_request_local_store_opt_out_still_wins_after_reauthorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_store_policy(monkeypatch, tmp_path, "enabled")
    memory = _memory(tmp_path)
    query = "Покажи активные заказы магазина, но магазин пока не трогай"

    assert find_command_route(query) is None
    brief = build_agent_brief(memory, query, limit=5)
    assert brief["runtime_policies"]["autostop_store"]["state"] == "enabled"
    assert brief["runtime_policies"]["autostop_store"]["query_scope_excluded"] is True
    assert brief["runtime_policies"]["autostop_store"]["effective_paused"] is True
    assert "store_api" not in brief["memory_sources"]


def test_paused_automotive_and_service_briefs_keep_crm_oem_and_public_routes_without_store_api(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    automotive = build_agent_brief(memory, "Как выставить ГРМ на Mercedes M274?", limit=20)
    service = build_agent_brief(memory, "оцени стоимость ремонта Ford Focus замена сцепления", limit=20)

    assert automotive["route"]["domain"] == "automotive_repair"
    assert service["route"]["domain"] == "work_labor_pricing"
    assert any("VIN/OEM" in item for item in automotive["allowed_actions"])
    assert any("public-market" in item for item in automotive["allowed_actions"])
    assert any("AutoStop CRM" in item for item in service["route"]["external_connectors"])
    assert any("public automotive" in item for item in service["route"]["external_connectors"])

    active_guidance = "\n".join(
        [
            *automotive["read_order"],
            *automotive["allowed_actions"],
            *service["route"]["required_reads"],
            *service["route"]["external_connectors"],
            *service["next_actions"],
        ]
    ).casefold()
    assert "autostop app" not in active_guidance
    assert "supplier api" not in active_guidance
    assert "compare live supplier" not in active_guidance

    service_workflow = next(
        item
        for item in list_agent_workflows(query="", limit=100)["summary"]["items"]
        if item["workflow_id"] == "adaptive_service_case"
    )
    workflow_guidance = "\n".join(
        [
            str(service_workflow.get("open_first") or ""),
            *service_workflow["required_reads"],
            *service_workflow["external_connectors"],
            *service_workflow["completion_checks"],
        ]
    ).casefold()
    assert "autostop app" not in workflow_guidance
    assert "supplier" not in workflow_guidance

    selected = build_agent_bootstrap(memory, query="оцени стоимость ремонта Ford Focus замена сцепления")["summary"][
        "selected_workflow"
    ]
    selected_guidance = "\n".join(
        [
            str(selected.get("open_first") or ""),
            *selected["required_reads"],
            *selected["external_connectors"],
        ]
    ).casefold()
    assert "autostop app" not in selected_guidance
    assert "supplier" not in selected_guidance


def test_paused_brief_filters_stale_store_guidance_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context_module,
        "prepare_manager_context",
        lambda *_args, **_kwargs: {
            "query": "проверь автомобиль",
            "intent": None,
            "next_actions": ["fallback to AutoStop App"],
            "knowledge": {
                "has_knowledge": True,
                "best_domain": "automotive_repair",
                "open_first": "docs/agent/store_management_playbook.md",
                "source_of_truth": ["CRM", "docs/agent/store_management_playbook.md"],
                "reference_files": ["docs/agent/automotive_repair_source_playbook.md", "AutoStop App"],
                "optional_runtime_files": ["CRM", "docs/agent/store_management_playbook.md"],
                "optional_available_files": ["CRM", "docs/agent/store_management_playbook.md"],
                "optional_missing_files": ["CRM", "docs/agent/store_management_playbook.md"],
            },
            "command_route": {
                "domain": "automotive_repair",
                "required_reads": ["AutoStop App"],
                "next_actions": ["use AutoStop App"],
                "external_connectors": ["AutoStop App"],
                "completion_checks": ["AutoStop App"],
                "write_domains": ["store_sourcing_offer"],
                "operation_selection": {"store_read": {"aliases": ["проверь"]}},
            },
        },
    )

    brief = context_module.build_agent_brief(None, "проверь автомобиль")

    assert brief["route"]["open_first"] is None
    for field in (
        "source_of_truth",
        "reference_files",
        "optional_runtime_files",
        "optional_available_files",
        "optional_missing_files",
        "required_reads",
        "external_connectors",
        "completion_checks",
        "write_domains",
    ):
        assert all(
            "autostop app" not in item.casefold() and "store_" not in item.casefold() for item in brief["route"][field]
        )
    assert brief["route"]["operation_selection"] == {}
    assert brief["route"]["selected_operation"] is None
    assert brief["next_actions"] == []


def test_invalid_runtime_policy_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_store_policy(monkeypatch, tmp_path, "reopen-when-ready")

    policy = runtime_policy.get_store_access_policy()
    assert policy.paused is True
    assert policy.valid is False
    assert policy.reason == "store_policy_state_invalid"
    assert find_command_route("Покажи состояние склада") is None


def test_store_contract_and_workflow_execution_fail_closed(tmp_path: Path) -> None:
    contract = prepare_action_contract(
        domain="store_order",
        action="mark_order_ready",
        target_id="order-1",
        planned_changes={"status": "READY"},
        owner_intent="mark order ready",
        expected_revision="v1",
        idempotency_key="store-order-ready-policy-test",
    )
    memory = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    workflow = memory.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-workflow-policy-test",
    )

    assert contract["ok"] is False
    assert "store_access_paused" in contract["preflight"]["blocking_reasons"]
    assert contract["execution"]["ready"] is False
    assert workflow == {"ok": False, "status": "blocked", "error": "store_access_paused"}
    assert memory.list_active_manager_runs()["items"] == []


def test_paused_bootstrap_hides_prior_store_runs_and_reauthorization_restores_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_store_policy(monkeypatch, tmp_path, "enabled")
    memory = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store_run = memory.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-bootstrap-policy-test",
        scope={"domain": "store_order", "target_id": "order-1", "operation": "mark_order_ready"},
    )
    crm_run = memory.start_workflow_run(
        workflow_id="crm_finance_operation",
        intent="crm_finance_operation",
        idempotency_key="crm-bootstrap-policy-test",
    )
    assert store_run["ok"] is True
    assert crm_run["ok"] is True

    _set_store_policy(monkeypatch, tmp_path, "paused")
    paused = build_agent_bootstrap(memory, query="Приберись")["summary"]["unfinished_runs"]

    assert [item["run_id"] for item in paused] == [crm_run["id"]]

    _set_store_policy(monkeypatch, tmp_path, "enabled")
    reauthorized = build_agent_bootstrap(memory, query="Приберись")["summary"]["unfinished_runs"]

    assert {item["run_id"] for item in reauthorized} == {crm_run["id"], store_run["id"]}


def test_paused_policy_blocks_direct_lifecycle_access_to_existing_store_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_store_policy(monkeypatch, tmp_path, "enabled")
    memory = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = memory.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-direct-lifecycle-policy-test",
        scope={"domain": "store_order", "target_id": "order-1", "operation": "mark_order_ready"},
    )
    assert started["ok"] is True
    run_id = int(started["id"])

    _set_store_policy(monkeypatch, tmp_path, "paused")
    blocked_results = [
        memory.get_manager_run(run_id),
        memory.transition_workflow_run(run_id, status="executing", expected_state_version=1),
        memory.checkpoint_workflow_run(run_id, checkpoint={}, expected_state_version=1),
        memory.register_external_step(
            run_id,
            step_id="external-1",
            connector="gmail",
            action="send",
            request_refs={"external_ref": "ref-1"},
            expected_state_version=1,
        ),
        memory.complete_external_step(
            run_id,
            step_id="external-1",
            result_refs={"external_ref": "ref-1"},
            expected_state_version=1,
        ),
        memory.resume_workflow_run(run_id, expected_state_version=1),
        memory.cancel_workflow_run(run_id, expected_state_version=1),
    ]
    assert all(result.get("error") == "store_access_paused" for result in blocked_results)
    assert memory.list_active_manager_runs()["items"] == []
    assert memory.list_manager_runs()["items"] == []

    _set_store_policy(monkeypatch, tmp_path, "enabled")
    restored = memory.get_manager_run(run_id)
    assert restored["ok"] is True
    assert memory.transition_workflow_run(run_id, status="executing", expected_state_version=1)["ok"] is True
