from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from autostop_manager.storage import ManagerMemoryStore


HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agent_learning_hook.py"
SPEC = importlib.util.spec_from_file_location("agent_learning_hook_test", HOOK_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class FakeLearningStore:
    def __init__(self, mode: str = "learning") -> None:
        self.mode = mode
        self.started: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.review_complete = False
        self.fast_turn_lookup_calls = 0
        self.legacy_turn_lookup_calls = 0
        self.initialize_calls = 0

    def initialize(self) -> None:
        self.initialize_calls += 1
        raise AssertionError("work-mode tool hooks must not initialize learning storage")

    def resolve_agent_mode(self, mode_override: str | None = None) -> dict[str, str]:
        return {"effective_mode": mode_override or self.mode}

    def start_agent_turn(self, task_signature: str, **kwargs: Any) -> dict[str, str]:
        self.started.append({"task_signature": task_signature, **kwargs})
        return {
            "turn_id": "internal-turn",
            "external_turn_id": kwargs["external_turn_id"],
            "effective_mode": "learning",
        }

    def get_agent_turn_by_external_id(self, external_turn_id: str) -> dict[str, str] | None:
        self.legacy_turn_lookup_calls += 1
        if not self.started:
            return None
        return {"turn_id": "internal-turn", "external_turn_id": external_turn_id, "effective_mode": "learning"}

    def get_agent_learning_turn_by_external_id_fast(self, external_turn_id: str) -> dict[str, Any]:
        self.fast_turn_lookup_calls += 1
        if self.mode != "learning" or not self.started:
            return {"ok": True, "item": None}
        return {
            "ok": True,
            "item": {"turn_id": "internal-turn", "external_turn_id": external_turn_id, "effective_mode": "learning"},
        }

    def record_agent_tool_event(
        self,
        turn_id: str,
        tool_name: str,
        status: str,
        duration_ms: int | None = None,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        self.events.append(
            {
                "turn_id": turn_id,
                "tool_name": tool_name,
                "status": status,
                "duration_ms": duration_ms,
                "error_code": error_code,
                "metadata": metadata or {},
            }
        )
        return {"ok": True}

    def has_completed_experience_review_by_external_id(self, external_turn_id: str) -> bool:
        return self.review_complete

    def post_run_review(self, turn_id: str, **kwargs: Any) -> dict[str, bool]:
        self.reviews.append({"turn_id": turn_id, **kwargs})
        self.review_complete = True
        return {"ok": True}


def _prompt_payload(prompt: str = "проверь заказ-наряд в режиме обучения") -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn-123",
        "session_id": "session-456",
        "prompt": prompt,
    }


def test_learning_prompt_creates_opaque_turn_without_raw_prompt() -> None:
    store = FakeLearningStore()
    secret_prompt = "VIN WDB123; phone +79990000000; do not persist this raw prompt"

    result = HOOK.handle_hook(_prompt_payload(secret_prompt), store)

    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "learning" in result["hookSpecificOutput"]["additionalContext"]
    assert "turn-123" in result["hookSpecificOutput"]["additionalContext"]
    assert len(store.started) == 1
    stored = json.dumps(store.started[0], ensure_ascii=False)
    assert secret_prompt not in stored
    assert "WDB123" not in stored
    assert "+79990000000" not in stored
    assert store.started[0]["external_turn_id"] == "turn-123"
    assert len(store.started[0]["task_signature"]) == 64


def test_work_mode_does_not_create_learning_turn() -> None:
    store = FakeLearningStore(mode="work")

    result = HOOK.handle_hook(_prompt_payload("проверь карточку"), store)

    assert "work" in result["hookSpecificOutput"]["additionalContext"]
    assert store.started == []


def test_work_mode_tool_hooks_use_only_the_no_ddl_fast_lookup() -> None:
    store = FakeLearningStore(mode="work")
    payload = {"turn_id": "turn-work-123", "tool_name": "Bash", "tool_use_id": "tool-work-456"}

    HOOK.handle_hook({"hook_event_name": "PreToolUse", **payload}, store)
    HOOK.handle_hook({"hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}, **payload}, store)

    assert store.events == []
    assert store.fast_turn_lookup_calls == 2
    assert store.legacy_turn_lookup_calls == 0
    assert store.initialize_calls == 0


def test_explicit_work_override_wins_over_learning_default() -> None:
    store = FakeLearningStore(mode="learning")

    result = HOOK.handle_hook(_prompt_payload("эту задачу выполни в рабочем режиме"), store)

    assert "work" in result["hookSpecificOutput"]["additionalContext"]
    assert store.started == []


def test_negative_learning_instruction_is_a_work_override() -> None:
    assert HOOK.explicit_mode_override("не включай режим обучения для этой задачи") == "work"


def test_tool_events_store_only_safe_metadata() -> None:
    store = FakeLearningStore()
    HOOK.handle_hook(_prompt_payload(), store)
    raw_command = "curl https://example.invalid/?vin=WDB123&phone=7999"
    raw_response = {"exit_code": 1, "output": "secret response WDB123"}

    HOOK.handle_hook(
        {
            "hook_event_name": "PreToolUse",
            "turn_id": "turn-123",
            "tool_name": "Bash",
            "tool_use_id": "tool-789",
            "tool_input": {"command": raw_command},
        },
        store,
    )
    HOOK.handle_hook(
        {
            "hook_event_name": "PostToolUse",
            "turn_id": "turn-123",
            "tool_name": "Bash",
            "tool_use_id": "tool-789",
            "tool_input": {"command": raw_command},
            "tool_response": raw_response,
        },
        store,
    )

    assert [event["status"] for event in store.events] == ["started", "failed"]
    assert store.fast_turn_lookup_calls == 2
    assert store.legacy_turn_lookup_calls == 0
    assert store.events[1]["error_code"] == "nonzero_exit"
    assert store.events[1]["metadata"]["response_shape"] == "object"
    serialized = json.dumps(store.events, ensure_ascii=False)
    assert raw_command not in serialized
    assert "secret response" not in serialized
    assert "WDB123" not in serialized


def test_stop_blocks_once_then_deferred_review_prevents_loop() -> None:
    store = FakeLearningStore()
    HOOK.handle_hook(_prompt_payload(), store)

    first = HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-123", "stop_hook_active": False}, store)
    second = HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-123", "stop_hook_active": True}, store)

    assert first["decision"] == "block"
    assert second == {}
    assert len(store.reviews) == 1
    assert store.reviews[0]["outcome"] == "deferred"
    assert store.reviews[0]["completion_checks"] == ["learning_review_deferred_after_stop_continuation"]


def test_stop_blocks_pending_improvement_until_it_is_deferred_or_rolled_back(tmp_path: Path) -> None:
    store = ManagerMemoryStore(tmp_path / "manager.sqlite3")
    assert store.set_agent_mode("learning")["ok"] is True

    defer_payload = {**_prompt_payload("проверь маршрут в режиме обучения"), "turn_id": "turn-pending-defer"}
    HOOK.handle_hook(defer_payload, store)
    defer_review = store.post_run_review(
        "turn-pending-defer",
        improvement_kind="route",
        risk="low",
        metadata={"review_status": "completed"},
    )
    defer_candidate = defer_review["improvement"]["id"]
    assert store.has_completed_experience_review_by_external_id("turn-pending-defer")["learning_cycle_closed"] is False
    assert (
        HOOK.handle_hook(
            {"hook_event_name": "Stop", "turn_id": "turn-pending-defer", "stop_hook_active": False}, store
        )["decision"]
        == "block"
    )
    assert (
        store.agent_learning_workflow(
            "defer",
            candidate_id=defer_candidate,
            reason_code="external_provider_follow_up",
        )["improvement"]["status"]
        == "deferred"
    )
    assert (
        HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-pending-defer", "stop_hook_active": False}, store)
        == {}
    )

    rollback_payload = {**_prompt_payload("проверь маршрут в режиме обучения"), "turn_id": "turn-pending-rollback"}
    HOOK.handle_hook(rollback_payload, store)
    rollback_review = store.post_run_review(
        "turn-pending-rollback",
        improvement_kind="route",
        risk="low",
        metadata={"review_status": "completed"},
    )
    rollback_candidate = rollback_review["improvement"]["id"]
    assert store.agent_learning_workflow("repair", candidate_id=rollback_candidate)["ok"] is True
    assert (
        store.agent_learning_workflow("rollback", candidate_id=rollback_candidate)["improvement"]["status"]
        == "rolled_back"
    )
    assert (
        HOOK.handle_hook(
            {"hook_event_name": "Stop", "turn_id": "turn-pending-rollback", "stop_hook_active": False}, store
        )
        == {}
    )


def test_stop_second_pass_defers_an_existing_pending_improvement(tmp_path: Path) -> None:
    store = ManagerMemoryStore(tmp_path / "manager.sqlite3")
    assert store.set_agent_mode("learning")["ok"] is True
    payload = {**_prompt_payload("проверь маршрут в режиме обучения"), "turn_id": "turn-pending-auto-defer"}
    HOOK.handle_hook(payload, store)
    review = store.post_run_review(
        "turn-pending-auto-defer",
        improvement_kind="route",
        risk="low",
        metadata={"review_status": "completed"},
    )
    candidate_id = review["improvement"]["id"]

    assert (
        HOOK.handle_hook(
            {"hook_event_name": "Stop", "turn_id": "turn-pending-auto-defer", "stop_hook_active": False}, store
        )["decision"]
        == "block"
    )
    assert (
        HOOK.handle_hook(
            {"hook_event_name": "Stop", "turn_id": "turn-pending-auto-defer", "stop_hook_active": True}, store
        )
        == {}
    )
    candidate = store.agent_learning_workflow("summary")["recent_improvements"]
    assert next(item for item in candidate if item["id"] == candidate_id)["status"] == "deferred"
    assert (
        store.has_completed_experience_review_by_external_id("turn-pending-auto-defer")["learning_cycle_closed"] is True
    )


def test_stop_is_open_when_learning_store_is_unavailable() -> None:
    assert HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-123"}, store=None) == {}


def test_hook_uses_the_learning_storage_contract_without_raw_values(tmp_path: Path) -> None:
    store = ManagerMemoryStore(tmp_path / "manager.sqlite3")
    assert store.set_agent_mode("learning")["ok"] is True
    prompt = "Проверь заказ-наряд и VIN WDB12345678901234"

    result = HOOK.handle_hook(_prompt_payload(prompt), store)
    HOOK.handle_hook(
        {
            "hook_event_name": "PreToolUse",
            "turn_id": "turn-123",
            "tool_name": "mcp__autostopcrm__agent_entity_context",
            "tool_use_id": "tool-789",
            "tool_input": {"vin": "WDB12345678901234"},
        },
        store,
    )
    HOOK.handle_hook(
        {
            "hook_event_name": "PostToolUse",
            "turn_id": "turn-123",
            "tool_name": "mcp__autostopcrm__agent_entity_context",
            "tool_use_id": "tool-789",
            "tool_response": {"ok": True, "vehicle": {"vin": "WDB12345678901234"}},
        },
        store,
    )

    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    turn = store.get_agent_turn_by_external_id("turn-123")
    assert turn["ok"] is True
    item = turn["item"]
    assert item["effective_mode"] == "learning"
    assert len(item["tool_events"]) == 2
    serialized = json.dumps(item, ensure_ascii=False)
    assert prompt not in serialized
    assert "WDB12345678901234" not in serialized

    assert (
        HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-123", "stop_hook_active": False}, store)[
            "decision"
        ]
        == "block"
    )
    assert HOOK.handle_hook({"hook_event_name": "Stop", "turn_id": "turn-123", "stop_hook_active": True}, store) == {}
    review = store.has_completed_experience_review_by_external_id("turn-123")
    assert review["review_completed"] is True
    assert review["outcome"] == "deferred"
