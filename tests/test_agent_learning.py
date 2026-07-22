from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json

from autostop_manager import cli
from autostop_manager.agent_gateway import build_agent_bootstrap
from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore, _normalize_learning_identifier


class _FakeServer:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, name: str, description: str = "", **_kwargs):
        del description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_agent_mode_defaults_to_work_and_resolves_per_turn_override(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    initial = store.get_agent_mode()
    assert initial["global_mode"] == "work"
    assert store.resolve_agent_mode("learning")["effective_mode"] == "learning"

    enabled = store.set_agent_mode("learning", expected_state_version=initial["state_version"])
    assert enabled["ok"] is True
    assert enabled["global_mode"] == "learning"
    assert store.resolve_agent_mode("work")["effective_mode"] == "work"

    stale = store.set_agent_mode("work", expected_state_version=initial["state_version"])
    assert stale["error"] == "agent_mode_state_conflict"


def test_agent_mode_concurrent_compare_and_swap_has_one_winner(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    initial = ManagerMemoryStore(db_path).get_agent_mode()
    barrier = Barrier(2)

    def set_learning() -> dict:
        local_store = ManagerMemoryStore(db_path)
        # Complete migration before synchronizing the actual competing writes.
        local_store.get_agent_mode()
        barrier.wait(timeout=5)
        return local_store.set_agent_mode("learning", expected_state_version=initial["state_version"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10) for future in (executor.submit(set_learning), executor.submit(set_learning))
        ]

    assert sum(result.get("ok") is True for result in results) == 1
    assert sum(result.get("error") == "agent_mode_state_conflict" for result in results) == 1


def test_learning_identifier_accepts_opaque_uuid_and_hash_refs() -> None:
    candidate_uuid = "12345678-1234-4abc-8def-1234567890ab"

    assert _normalize_learning_identifier(candidate_uuid, field="candidate_id", allow_empty=False) == candidate_uuid
    assert _normalize_learning_identifier("a" * 64, field="safe_ref", allow_empty=False) == "a" * 64


def test_learning_ledger_hashes_task_and_rejects_raw_business_metadata(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = ManagerMemoryStore(db_path)
    assert store.set_agent_mode("learning")["ok"] is True

    raw_prompt = "Проверь CRM клиента, VIN WDB12345678901234, телефон +79990000000"
    rejected = store.start_agent_turn(raw_prompt, metadata={"vin": "WDB12345678901234"})
    assert rejected["error"] == "raw_agent_learning_payload_not_allowed"

    started = store.start_agent_turn(
        raw_prompt,
        external_turn_id="codex-turn-1",
        workflow_id="crm_card_cleanup",
        metadata={"source_kind": "test", "safe_ref": "a" * 64},
    )
    assert started["ok"] is True
    assert started["task_signature"].startswith("sha256:")
    assert raw_prompt not in db_path.read_text(encoding="utf-8", errors="ignore")

    unsafe_fingerprint = store.start_agent_turn(
        "another safe task",
        external_turn_id="codex-turn-unsafe-fingerprint",
        metadata={"request_fingerprint": "crm-WDB12345678901234"},
    )
    assert unsafe_fingerprint["error"] == "raw_agent_learning_payload_not_allowed"

    raw_event = store.record_agent_tool_event(
        "codex-turn-1",
        tool_name="agent_entity_context",
        status="started",
        metadata={"phase": "pre", "customer_phone": "+79990000000"},
    )
    assert raw_event["error"] == "raw_agent_learning_payload_not_allowed"

    assert (
        store.record_agent_tool_event(
            "codex-turn-1",
            tool_name="agent_entity_context",
            status="started",
            metadata={"phase": "pre", "tool_use_id_hash": "b" * 64},
        )["ok"]
        is True
    )
    completed_event = store.record_agent_tool_event(
        "codex-turn-1",
        tool_name="agent_entity_context",
        status="succeeded",
        metadata={"phase": "post", "tool_use_id_hash": "b" * 64, "response_shape": "object"},
    )
    assert completed_event["ok"] is True
    assert completed_event["duration_ms"] is not None

    review = store.post_run_review(
        "codex-turn-1",
        completion_checks=["crm_readback_passed"],
        tool_assessment=[{"tool_name": "agent_entity_context", "status": "succeeded", "calls": 1}],
        improvement_kind="route",
        risk="low",
        metadata={"review_status": "completed"},
    )
    assert review["ok"] is True
    assert review["review"]["outcome"] == "confirmed"
    candidate_id = review["improvement"]["id"]

    duplicate = store.post_run_review("codex-turn-1", outcome="failed")
    assert duplicate["deduplicated"] is True
    review_state = store.has_completed_experience_review_by_external_id("codex-turn-1")
    assert review_state["review_completed"] is True
    assert review_state["learning_cycle_closed"] is False
    assert review_state["unresolved_improvement_ids"] == [candidate_id]

    assert store.agent_learning_workflow("repair", candidate_id=candidate_id)["improvement"]["status"] == "repairing"
    verified = store.agent_learning_workflow(
        "verify",
        candidate_id=candidate_id,
        verification={"verification_state": "passed", "test_ref": "pytest-agent-learning"},
    )
    assert verified["improvement"]["status"] == "verified"
    unsafe_lesson = store.agent_learning_workflow(
        "promote",
        candidate_id=candidate_id,
        lesson_title="Технический урок",
        lesson_content="Для WDB12345678901234 стоимость 12 000 руб. нельзя сохранять в обучении.",
        applies_to="crm_card_cleanup",
    )
    assert unsafe_lesson["error"] == "unsafe_agent_learning_lesson"
    unsafe_name_lesson = store.agent_learning_workflow(
        "promote",
        candidate_id=candidate_id,
        lesson_title="Технический урок",
        lesson_content="Иван Петров не должен попадать в опыт менеджера.",
        applies_to="crm_card_cleanup",
    )
    assert unsafe_name_lesson["error"] == "unsafe_agent_learning_lesson"
    promoted = store.agent_learning_workflow(
        "promote",
        candidate_id=candidate_id,
        lesson_title="Проверенный маршрут CRM",
        lesson_content="После readback использовать проверенный маршрут CRM и фиксировать только технический итог.",
        applies_to="crm_card_cleanup",
    )
    assert promoted["improvement"]["status"] == "promoted"
    assert store.has_completed_experience_review_by_external_id("codex-turn-1")["learning_cycle_closed"] is True
    assert store.recall_lessons("проверенный маршрут", applies_to="crm_card_cleanup")["items"]

    raw_db = db_path.read_bytes()
    assert raw_prompt.encode() not in raw_db
    assert b"WDB12345678901234" not in raw_db
    assert b"+79990000000" not in raw_db
    assert b"12 000" not in raw_db


def test_work_turn_is_skipped_and_legacy_mode_turn_is_not_reused(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    skipped = store.start_agent_turn("ordinary work", external_turn_id="mode-transition-turn")
    assert skipped["ok"] is True
    assert skipped["skipped"] is True
    assert skipped["turn_id"] is None
    assert (
        store.get_active_agent_turn(
            "ordinary work",
            external_turn_id="mode-transition-turn",
            effective_mode="learning",
        )["active_turn"]
        is None
    )

    assert store.set_agent_mode("learning")["ok"] is True
    legacy = store.start_agent_turn("ordinary work", external_turn_id="mode-transition-turn")
    with store.connect() as conn:
        conn.execute("UPDATE agent_turns SET effective_mode = 'work' WHERE id = ?", (legacy["turn_id"],))

    boot = build_agent_bootstrap(
        store,
        query="ordinary work",
        external_turn_id="mode-transition-turn",
    )
    active_turn = boot["summary"]["agent_mode"]["active_turn"]
    assert active_turn["turn_id"] != legacy["turn_id"]
    assert active_turn["effective_mode"] == "learning"
    assert store.post_run_review(active_turn["turn_id"], metadata={"review_status": "completed"})["ok"] is True


def test_fast_learning_turn_lookup_does_not_run_schema_initializer(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    ManagerMemoryStore(db_path).get_agent_mode()

    class NoInitializeStore(ManagerMemoryStore):
        def initialize(self) -> None:
            raise AssertionError("fast hook lookup must not run schema initialization")

    result = NoInitializeStore(db_path).get_agent_learning_turn_by_external_id_fast("no-learning-turn")
    assert result == {"ok": True, "item": None}


def test_learning_bootstrap_creates_one_active_turn_and_work_does_not(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    assert store.set_agent_mode("learning")["ok"] is True

    first = build_agent_bootstrap(store, query="проверь описание карточки CRM")
    first_turn = first["summary"]["agent_mode"]["active_turn"]
    assert first["summary"]["agent_mode"]["effective_mode"] == "learning"
    assert first_turn["turn_id"]
    assert first["meta"]["case_resolver_tool"] == "agent_case_resolver"

    second = build_agent_bootstrap(store, query="проверь описание карточки CRM")
    assert second["summary"]["agent_mode"]["active_turn"]["turn_id"] == first_turn["turn_id"]

    assert store.set_agent_mode("work")["ok"] is True
    work = build_agent_bootstrap(store, query="проверь описание карточки CRM")
    assert work["summary"]["agent_mode"]["effective_mode"] == "work"
    assert work["summary"]["agent_mode"]["active_turn"] is None
    assert sum(row["count"] for row in store.get_agent_learning_summary()["turn_counts"]) == 1


def test_learning_repair_refuses_high_risk_and_provider_candidates(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    assert store.set_agent_mode("learning")["ok"] is True

    high_turn = store.start_agent_turn("high-risk-local", external_turn_id="learning-high")
    high_review = store.post_run_review(
        high_turn["turn_id"],
        improvement_kind="code",
        risk="high",
        metadata={"review_status": "completed"},
    )
    high_candidate = high_review["improvement"]["id"]
    assert store.agent_learning_workflow("repair", candidate_id=high_candidate)["error"] == (
        "agent_improvement_repair_requires_low_risk"
    )

    provider_turn = store.start_agent_turn("provider-outage", external_turn_id="learning-provider")
    provider_review = store.post_run_review(
        provider_turn["turn_id"],
        improvement_kind="provider",
        risk="low",
        metadata={"review_status": "completed"},
    )
    provider_candidate = provider_review["improvement"]["id"]
    assert store.agent_learning_workflow("repair", candidate_id=provider_candidate)["error"] == (
        "agent_provider_improvement_must_be_deferred"
    )


def test_learning_mcp_tools_and_cli_switch_use_safe_storage(tmp_path, monkeypatch, capsys):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    server = _FakeServer()
    register_manager_memory_tools(server, store)

    assert {"agent_mode", "post_run_review", "agent_learning_workflow"}.issubset(server.tools)
    assert server.tools["agent_mode"]("set", mode="learning")["global_mode"] == "learning"
    assert server.tools["agent_mode"]("resolve", mode_override="work")["effective_mode"] == "work"

    monkeypatch.setattr(cli, "ManagerMemoryStore", lambda: store)
    assert cli.main(["agent-mode", "set", "work"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["global_mode"] == "work"
    assert cli.main(["agent-mode", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["global_mode"] == "work"
