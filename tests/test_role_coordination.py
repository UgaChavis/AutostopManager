from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.exceptions import ConnectionClosed

from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager import instruction_bundle as instruction_bundles
from autostop_manager.instruction_bundle import load_instruction_bundle
from autostop_manager.role_coordination import MODEL, PROJECT_DIR, RoleError, RoleStore
from autostop_manager.role_executor import RoleExecutor, RoleRpcRejected, role_objective
from tests.instruction_bundle_fakes import create_instruction_bundle, seal_instruction_bundle


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(instruction_bundles, "_OWNER_UID", os.getuid())
    root = create_instruction_bundle(tmp_path / "snapshot")
    resolve = Path.resolve

    def synthetic_current(path, *args, **kwargs):
        if str(path) == "/opt/autostop-manager-releases/current":
            return root
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", synthetic_current)
    result = RoleStore(tmp_path / "scheduler" / "registry.sqlite3")
    for role in ("M1", "M2"):
        result.action(
            "bind",
            role,
            {"thread_id": str(uuid4()), "instruction_root": str(root), "instruction_revision": "a" * 40},
            idempotency_key=f"bind-{role}",
            expected_revision=0,
        )
    return result


def action(store, operation, role="M1", payload=None, *, actor="owner", revision=None, key=None):
    return store.action(
        operation,
        role,
        payload,
        idempotency_key=key or str(uuid4()),
        actor=actor,
        expected_revision=store.role_state(role)["revision"] if revision is None else revision,
    )


class FakeRPC:
    def __init__(self):
        self.turns = {}
        self.goals = {}
        self.calls = []
        self.lose_start = False
        self.config = {"skills": {"config": [{"path": "/synthetic/skills/disabled", "enabled": False}]}}
        self.thread_configs = {}
        self.injected = {}
        self.injection_error = None

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "config/read":
            assert params == {"cwd": PROJECT_DIR, "includeLayers": False}
            return {"config": deepcopy(self.config)}
        thread = params["threadId"]
        turns = self.turns.setdefault(thread, [])
        if method in {"thread/resume", "thread/read"}:
            if method == "thread/resume":
                self.thread_configs.setdefault(thread, {}).update(params.get("config", {}))
            return {
                "model": MODEL,
                "thread": {
                    "id": thread,
                    "cwd": PROJECT_DIR,
                    "ephemeral": False,
                    "status": {"type": "active" if any(t["status"] == "inProgress" for t in turns) else "idle"},
                },
            }
        if method == "thread/inject_items":
            self.injected.setdefault(thread, []).extend(params["items"])
            if self.injection_error is not None:
                raise self.injection_error
            return {}
        if method == "thread/turns/list":
            offset = int(params.get("cursor") or 0)
            end = offset + params["limit"]
            return {"data": turns[offset:end], "nextCursor": str(end) if len(turns) > end else None}
        if method == "thread/goal/get":
            return {"goal": self.goals.get(thread)}
        if method == "thread/goal/set":
            self.goals[thread] = {**self.goals.get(thread, {}), **params}
            return {"goal": self.goals[thread]}
        if method == "turn/start":
            turn = {
                "id": str(uuid4()),
                "status": "inProgress",
                "items": [{"type": "userMessage", "clientId": params["clientUserMessageId"]}],
            }
            turns.insert(0, turn)
            if self.lose_start:
                self.lose_start = False
                raise TimeoutError("synthetic lost start response")
            return {"turn": turn}
        if method == "turn/steer":
            turn = next(t for t in turns if t["id"] == params["expectedTurnId"])
            turn["items"].append({"type": "userMessage", "clientId": params["clientUserMessageId"]})
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            next(t for t in turns if t["id"] == params["turnId"])["status"] = "interrupted"
            return {}
        raise AssertionError(method)


def executor(store, rpc):
    @asynccontextmanager
    async def factory():
        yield rpc

    return RoleExecutor(store, rpc_factory=factory)


def test_read_does_not_create_storage_and_both_duties_default_off(tmp_path):
    state = RoleStore(tmp_path / "missing" / "state.sqlite3")
    assert [r["desired_state"] for r in state.read()["roles"]] == ["off", "off"]
    assert state.read("tasks")["items"] == []
    assert not state.path.exists()


def test_same_scheduler_database_preserves_existing_automation_state(tmp_path):
    automation = AutomationStore(tmp_path / "registry.sqlite3")
    automation.initialize()
    roles = RoleStore(automation.path)
    roles.initialize()
    assert roles.role_state()["desired_state"] == "off"
    assert automation.status()["schema_version"]


def test_bind_is_exact_and_cannot_reuse_another_role_thread(store):
    with pytest.raises(RoleError, match="role_thread_already_bound"):
        action(store, "bind", "M2", {"thread_id": store.role_state("M1")["thread_id"]})


def test_idempotency_and_revision_prevent_stale_changes(store):
    revision = store.role_state()["revision"]
    first = action(store, "start", key="same-start", revision=revision)
    assert action(store, "start", key="same-start", revision=revision) == first
    with pytest.raises(RoleError, match="idempotency"):
        action(store, "stop", key="same-start", revision=revision)
    with pytest.raises(RoleError, match="revision_conflict"):
        action(store, "stop", revision=revision)


def test_m1_may_start_m2_and_stopping_m1_does_not_stop_m2(store):
    action(store, "start")
    action(store, "start", "M2", actor="M1")
    action(store, "stop")
    assert not store.role_state("M1")["duty_active"]
    assert store.role_state("M2")["duty_active"]
    with pytest.raises(RoleError, match="owner_required"):
        action(store, "stop", "M2", actor="M2")


@pytest.mark.parametrize("seconds", [0, 3601, True, "60"])
def test_sleep_is_bounded_without_disabling_duty(store, seconds):
    action(store, "start")
    with pytest.raises(RoleError, match="sleep_invalid"):
        action(store, "sleep", payload={"seconds": seconds})
    assert store.role_state()["duty_active"]


def test_one_engineer_claim_and_m1_does_not_duplicate_active_m2(store):
    task = action(
        store,
        "task_create",
        payload={
            "title": "Fix bug",
            "objective": "Fix synthetic bug",
            "resource": "manager:test.py",
            "acceptance": "Regression passes",
        },
    )["item"]
    action(store, "start", "M2")
    with pytest.raises(RoleError, match="m2_is_on_duty"):
        action(store, "task_claim", payload={"id": task["id"]}, revision=task["revision"], actor="M1")
    claimed = action(store, "task_claim", "M2", {"id": task["id"]}, revision=task["revision"], actor="M2")["item"]
    with pytest.raises(RoleError, match="resource_busy"):
        action(
            store,
            "task_create",
            payload={"title": "Duplicate", "objective": "Fix", "resource": "manager:test.py", "acceptance": "Pass"},
        )
    reviewed = action(
        store,
        "task_result",
        "M2",
        {
            "id": task["id"],
            "result": "Fixed",
            "verification": "Focused test passes",
            "artifact_ref": "git:abc",
            "release_required": True,
        },
        revision=claimed["revision"],
        actor="M2",
    )["item"]
    assert reviewed["status"] == "review"
    accepted = action(
        store,
        "task_accept",
        payload={"id": task["id"], "verification": "Read independent test result"},
        revision=reviewed["revision"],
        actor="M1",
    )["item"]
    assert accepted["status"] == "completed"


def test_approval_binds_exact_action_target_version_and_proposal(store):
    proposal = {
        "action": "deploy",
        "target": "manager",
        "target_revision": "old-sha",
        "proposal_ref": "git:new-sha",
        "proposal_hash": hashlib.sha256(b"specific release").hexdigest(),
    }
    pending = action(store, "approval_create", payload=proposal, actor="M1")["item"]
    with pytest.raises(RoleError, match="decision_denied"):
        action(
            store,
            "approval_decide",
            payload={"id": pending["id"], "decision": "approved", "decision_ref": "telegram:event"},
            actor="M1",
            revision=1,
        )
    approved = action(
        store,
        "approval_decide",
        payload={"id": pending["id"], "decision": "approved", "decision_ref": "telegram:event"},
        revision=1,
    )["item"]
    with pytest.raises(RoleError, match="stale"):
        action(
            store,
            "approval_consume",
            payload={**proposal, "id": pending["id"], "target_revision": "changed"},
            revision=approved["revision"],
        )
    assert (
        action(store, "approval_consume", payload={**proposal, "id": pending["id"]}, revision=approved["revision"])[
            "item"
        ]["status"]
        == "used"
    )


def test_learning_is_separate_from_permissions_and_preserves_m2_journal(store):
    directory = store.views_root / "M2"
    directory.mkdir(exist_ok=True)
    journal = directory / "current-state.md"
    journal.write_text("existing M2 technical state")
    action(store, "learn", payload={"text": "Verified practice", "source_ref": "git:abc"}, actor="M1")
    with pytest.raises(RoleError, match="owner_required"):
        action(
            store, "permission_record", payload={"text": "More authority", "source_ref": "observation:1"}, actor="M1"
        )
    assert store.read("notes")["items"][0]["kind"] == "knowledge"
    assert not store.read("permissions")["items"]
    assert journal.read_text() == "existing M2 technical state"


def test_owner_one_shot_works_when_duties_off_without_starting_goal(store):
    rpc = FakeRPC()
    receipt = store.submit_owner_event("event-1")
    assert store.submit_owner_event("event-1") == receipt
    run = executor(store, rpc)
    asyncio.run(run.tick())
    assert store.owner_event_receipt("event-1")["status"] == "running"
    assert not rpc.goals
    assert not store.role_state()["duty_active"]
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert store.owner_event_receipt("event-1")["status"] == "completed"
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1


def test_lost_launch_is_matched_by_exact_client_id_without_replay(store):
    rpc = FakeRPC()
    rpc.lose_start = True
    store.submit_owner_event("event-unknown")
    run = executor(store, rpc)
    asyncio.run(run.tick())
    assert store.owner_event_receipt("event-unknown")["status"] == "unknown"
    asyncio.run(run.tick())
    assert store.owner_event_receipt("event-unknown")["status"] == "running"
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1


def test_both_native_goals_are_independent_and_terra_is_used(store):
    action(store, "start", "M1")
    action(store, "start", "M2")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    assert len(rpc.goals) == 2
    assert all(params["model"] == MODEL for method, params in rpc.calls if method == "turn/start")
    action(store, "stop", "M1")
    asyncio.run(run.tick())
    assert rpc.goals[store.role_state("M1")["thread_id"]]["status"] == "paused"
    assert rpc.goals[store.role_state("M2")["thread_id"]]["status"] == "active"


def test_owner_event_steers_existing_m1_instead_of_creating_second_worker(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    store.submit_owner_event("event-steer")
    asyncio.run(run.tick())
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1
    assert len([c for c in rpc.calls if c[0] == "turn/steer"]) == 1
    assert store.owner_event_receipt("event-steer")["turn_id"] == rpc.turns[store.role_state()["thread_id"]][0]["id"]


@pytest.mark.parametrize("status", ["usageLimited", "budgetLimited", "blocked", "paused"])
def test_native_limit_or_manual_pause_is_not_bypassed(store, status):
    action(store, "start")
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": status}
    store.runtime_update("M1", goal_owned=True, goal_applied="active")
    asyncio.run(executor(store, rpc).tick())
    assert not any(method in {"thread/goal/set", "turn/start"} for method, _ in rpc.calls)
    assert store.role_state()["state"] == "attention"


def test_owner_submit_control_requires_system_transport(tmp_path):
    service = AutomationControlService(AutomationStore(tmp_path / "registry.sqlite3"))
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": "role_owner_submit",
        "actor": {"kind": "codex", "id": "test", "is_admin": True},
        "payload": {"event_id": "event-a"},
    }
    with pytest.raises(AutomationError, match="permission_denied"):
        service.handle(request)
    request["actor"]["kind"] = "system"
    assert service.handle(request)["status"] == "queued"


def test_stop_all_is_atomic_and_checks_both_revisions(store):
    action(store, "start", "M1")
    action(store, "start", "M2")
    with pytest.raises(RoleError, match="revision_conflict"):
        action(store, "stop_all", payload={"m2_revision": 0})
    assert all(state["duty_active"] for state in store.read()["roles"])
    action(store, "stop_all", payload={"m2_revision": store.role_state("M2")["revision"]})
    assert not any(state["duty_active"] for state in store.read()["roles"])


def test_role_binding_cannot_redirect_to_a_different_host(store):
    with pytest.raises(RoleError, match="host_mismatch"):
        action(store, "bind", payload={"thread_id": str(uuid4()), "host_id": "different-host"})
    assert store.role_state()["host_id"]


def test_sleep_pauses_and_resumes_existing_goal_automatically(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    action(store, "sleep", payload={"seconds": 3600}, actor="M1")
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert rpc.goals[thread]["origin"] == "automatic"
    assert store.role_state()["state"] == "waiting"
    store.runtime_update("M1", next_wake_at="2020-01-01T00:00:00+00:00")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "active"
    assert rpc.goals[thread]["origin"] == "automatic"


def test_stop_interrupts_native_goal_continuation_without_dispatch(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    original = rpc.turns[thread][0]
    original["status"] = "completed"
    continuation = {"id": str(uuid4()), "status": "inProgress", "items": []}
    rpc.turns[thread].insert(0, continuation)
    asyncio.run(run.tick())
    assert store.role_state()["active_dispatch_id"] is None
    action(store, "stop")
    asyncio.run(run.tick())
    assert continuation["status"] == "interrupted"
    assert rpc.goals[thread]["status"] == "paused"
    assert not store.role_state()["goal_owned"]  # The hold path independently reads the goal and terminal turn.
    asyncio.run(run.tick())
    assert not store.role_state()["goal_owned"]
    store.submit_owner_event("off-after-native-stop")
    asyncio.run(run.tick())
    assert store.owner_event_receipt("off-after-native-stop")["status"] == "running"
    assert not store.role_state()["duty_active"]


@pytest.mark.parametrize("rejected_method", ["thread/resume", "thread/turns/list", "thread/goal/get"])
def test_stop_after_rpc_rejection_confirms_owned_execution_without_restarting(store, rejected_method):
    class RejectRead(FakeRPC):
        reject = None

        async def request(self, method, params):
            if method == self.reject:
                self.reject = None
                self.calls.append((method, params))
                raise RoleRpcRejected("role_rpc_rejected")
            return await super().request(method, params)

    action(store, "start")
    rpc = RejectRead()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.reject = rejected_method
    asyncio.run(run.tick())
    assert store.role_state()["last_error"] == "role_rpc_rejected"
    action(store, "stop")
    # Stopping must not depend on the bundle that would be needed to start.
    root = Path(store.role_state()["instruction_root"])
    (root / "docs/agent/modules/F4.md").unlink()
    before = len(rpc.calls)
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert rpc.turns[thread][0]["status"] == "interrupted"
    state = store.role_state()
    assert state["desired_state"] == state["state"] == "off"
    assert state["last_error"] == "role_rpc_rejected"
    assert state["goal_applied"] == "paused" and not state["goal_owned"]
    assert not store.pending_dispatches("M1") and state["active_dispatch_id"] is None
    assert not any(method in {"thread/inject_items", "turn/start", "turn/steer"} for method, _ in rpc.calls[before:])
    assert len([method for method, _ in rpc.calls[before:] if method == "thread/goal/get"]) == 2
    assert len([method for method, _ in rpc.calls[before:] if method == "thread/turns/list"]) == 2


class RejectGoalAfterFirstTurn(FakeRPC):
    async def request(self, method, params):
        if method == "thread/goal/get" and self.turns.get(params["threadId"]):
            self.calls.append((method, params))
            raise RoleRpcRejected("role_rpc_rejected")
        return await super().request(method, params)


def first_duty_without_goal(store, rpc):
    action(store, "start")
    run = executor(store, rpc)
    asyncio.run(run.tick())
    state = store.role_state()
    assert state["last_error"] == "role_rpc_rejected" and not state["goal_owned"]
    assert store.pending_dispatches("M1")[0]["status"] == "running"
    assert not rpc.goals
    return state["thread_id"], run


def test_stop_known_first_duty_turn_without_native_goal_after_rpc_rejection(store):
    rpc = RejectGoalAfterFirstTurn()
    thread, _run = first_duty_without_goal(store, rpc)
    turn = rpc.turns[thread][0]
    action(store, "stop")
    before = len(rpc.calls)
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert turn["status"] == "interrupted"
    state = store.role_state()
    assert state["desired_state"] == state["state"] == "off" and not state["goal_owned"]
    assert state["last_error"] == "role_rpc_rejected"
    assert not store.pending_dispatches("M1") and state["active_dispatch_id"] is None
    assert [params for method, params in rpc.calls[before:] if method == "turn/interrupt"] == [
        {"threadId": thread, "turnId": turn["id"]}
    ]
    assert not any(
        method in {"thread/goal/get", "thread/goal/set", "thread/inject_items", "turn/start", "turn/steer"}
        for method, _ in rpc.calls[before:]
    )
    assert len([method for method, _ in rpc.calls[before:] if method == "thread/turns/list"]) == 2
    action(store, "bind", payload={"thread_id": str(uuid4())})


def test_first_duty_stop_requires_terminal_turn_readback_before_rebind(store):
    class PendingInterrupt(RejectGoalAfterFirstTurn):
        ignore_interrupt = True

        async def request(self, method, params):
            if method == "turn/interrupt" and self.ignore_interrupt:
                self.calls.append((method, params))
                return {}
            return await super().request(method, params)

    rpc = PendingInterrupt()
    thread, run = first_duty_without_goal(store, rpc)
    action(store, "stop")
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "inProgress"
    assert store.pending_dispatches("M1")
    with pytest.raises(RoleError, match="role_bind_requires_idle"):
        action(store, "bind", payload={"thread_id": str(uuid4())})
    rpc.ignore_interrupt = False
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "interrupted"
    assert not store.pending_dispatches("M1")


def test_first_duty_stop_reads_back_lost_interrupt_without_resending(store):
    class LostInterrupt(RejectGoalAfterFirstTurn):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "turn/interrupt":
                raise TimeoutError("synthetic lost interrupt response")
            return result

    rpc = LostInterrupt()
    thread, run = first_duty_without_goal(store, rpc)
    action(store, "stop")
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "interrupted"
    assert store.pending_dispatches("M1")
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert not store.pending_dispatches("M1")
    assert len([method for method, _ in rpc.calls if method == "turn/interrupt"]) == 1


def test_first_duty_stop_never_interrupts_foreign_turn_or_changes_foreign_goal(store):
    rpc = RejectGoalAfterFirstTurn()
    thread, run = first_duty_without_goal(store, rpc)
    rpc.turns[thread][0]["status"] = "completed"
    foreign = {"id": "foreign-turn", "status": "inProgress", "items": []}
    rpc.turns[thread].insert(0, foreign)
    rpc.goals[thread] = {"objective": "Unrelated user goal", "status": "active"}
    action(store, "stop")
    before = len(rpc.calls)
    asyncio.run(run.tick())
    assert foreign["status"] == "inProgress" and rpc.goals[thread]["status"] == "active"
    assert not store.pending_dispatches("M1")
    assert not any(
        method in {"thread/goal/set", "turn/interrupt", "thread/inject_items", "turn/start", "turn/steer"}
        for method, _ in rpc.calls[before:]
    )


def test_first_duty_rejected_interrupt_needs_new_control_revision_to_retry(store):
    class RejectedInterrupt(RejectGoalAfterFirstTurn):
        reject_interrupt = True

        async def request(self, method, params):
            if method == "turn/interrupt" and self.reject_interrupt:
                self.calls.append((method, params))
                raise RoleRpcRejected("synthetic rejected interrupt")
            return await super().request(method, params)

    rpc = RejectedInterrupt()
    thread, run = first_duty_without_goal(store, rpc)
    action(store, "stop")
    asyncio.run(run.tick())
    asyncio.run(run.tick())
    assert len([method for method, _ in rpc.calls if method == "turn/interrupt"]) == 1
    assert store.pending_dispatches("M1")
    action(store, "stop")
    rpc.reject_interrupt = False
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "interrupted" and not store.pending_dispatches("M1")


def test_first_duty_stop_does_not_override_explicit_recovery_during_readback(store):
    class RecoverDuringRead(RejectGoalAfterFirstTurn):
        recover = False

        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "thread/turns/list" and self.recover:
                self.recover = False
                action(store, "start")
            return result

    rpc = RecoverDuringRead()
    thread, run = first_duty_without_goal(store, rpc)
    action(store, "stop")
    rpc.recover = True
    before = len(rpc.calls)
    asyncio.run(run.tick())
    assert store.role_state()["duty_active"] and store.role_state()["last_error"] is None
    assert rpc.turns[thread][0]["status"] == "inProgress"
    assert not any(method == "turn/interrupt" for method, _ in rpc.calls[before:])


@pytest.mark.parametrize("owned,objective", [(False, role_objective("M1")), (True, "Unrelated user goal")])
def test_rejected_stop_does_not_change_unowned_or_foreign_execution(store, owned, objective):
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": objective, "status": "active"}
    rpc.turns[thread] = [{"id": "unrelated-turn", "status": "inProgress", "items": []}]
    store.runtime_update("M1", goal_owned=owned, goal_applied="active", last_error="role_rpc_rejected")
    action(store, "stop")
    asyncio.run(executor(store, rpc).tick())
    assert rpc.goals[thread]["status"] == "active"
    assert rpc.turns[thread][0]["status"] == "inProgress"
    assert store.role_state()["last_error"] == "role_rpc_rejected"
    assert not any(
        method in {"thread/goal/set", "turn/interrupt", "thread/inject_items", "turn/start", "turn/steer"}
        for method, _ in rpc.calls
    )


@pytest.mark.parametrize("failure_stage", ["open", "exit"])
def test_rejected_stop_retains_original_error_across_connection_failure(store, failure_stage):
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": "active"}
    store.runtime_update("M1", goal_owned=True, goal_applied="active", last_error="role_rpc_rejected")
    action(store, "stop")

    @asynccontextmanager
    async def failing_factory():
        if failure_stage == "open":
            raise ConnectionClosed(None, None)
        yield rpc
        raise ConnectionClosed(None, None)

    asyncio.run(RoleExecutor(store, rpc_factory=failing_factory).tick())
    assert store.role_state()["last_error"] == "role_rpc_rejected"
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert not store.role_state()["goal_owned"]
    assert store.role_state()["last_error"] == "role_rpc_rejected"


def test_rejected_stop_readback_does_not_restore_hold_after_explicit_recovery(store):
    class RecoverDuringPage(FakeRPC):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "thread/turns/list" and params.get("cursor"):
                action(store, "start")
            return result

    action(store, "start")
    rpc = RecoverDuringPage()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    rpc.turns[thread][:0] = [{"id": str(uuid4()), "status": "completed", "items": []} for _ in range(30)]
    store.runtime_update("M1", last_error="role_rpc_rejected")
    action(store, "stop")
    asyncio.run(run.tick())
    state = store.role_state()
    assert state["duty_active"] and state["last_error"] is None
    assert state["goal_applied"] is None and state["goal_owned"]
    assert len(store.pending_dispatches("M1")) == 1
    assert len([method for method, _ in rpc.calls if method == "turn/start"]) == 1


def test_bind_waits_for_independent_stop_confirmation_of_native_continuation(store):
    class PendingInterrupt(FakeRPC):
        ignore_interrupt = True

        async def request(self, method, params):
            if method == "turn/interrupt" and self.ignore_interrupt:
                self.calls.append((method, params))
                return {}
            return await super().request(method, params)

    action(store, "start")
    rpc = PendingInterrupt()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    rpc.turns[thread].insert(0, {"id": "native-continuation", "status": "inProgress", "items": []})
    asyncio.run(run.tick())
    assert not store.pending_dispatches("M1") and store.role_state()["active_dispatch_id"] is None
    action(store, "stop")
    before = store.role_state()
    replacement = {
        "thread_id": str(uuid4()),
        "instruction_root": before["instruction_root"],
        "instruction_revision": "a" * 40,
    }

    def assert_old_binding_held():
        with pytest.raises(RoleError, match="role_bind_requires_idle"):
            action(store, "bind", payload=replacement)
        state = store.role_state()
        assert state["goal_owned"]
        assert all(state[key] == before[key] for key in ("thread_id", "instruction_root", "instruction_revision"))

    assert_old_binding_held()
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "inProgress"
    assert_old_binding_held()
    rpc.ignore_interrupt = False
    asyncio.run(run.tick())
    assert rpc.turns[thread][0]["status"] == "interrupted"
    assert not store.role_state()["goal_owned"]
    action(store, "bind", payload=replacement)
    assert store.role_state()["thread_id"] == replacement["thread_id"]


def test_bind_between_native_turns_waits_for_independent_goal_pause(store):
    class IgnoredPause(FakeRPC):
        ignore_pause = True

        async def request(self, method, params):
            if method == "thread/goal/set" and params.get("status") == "paused" and self.ignore_pause:
                self.calls.append((method, params))
                return {}
            return await super().request(method, params)

    action(store, "start")
    rpc = IgnoredPause()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    action(store, "stop")
    replacement = {"thread_id": str(uuid4())}
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "active" and store.role_state()["goal_owned"]
    with pytest.raises(RoleError, match="role_bind_requires_idle"):
        action(store, "bind", payload=replacement)
    assert store.role_state()["thread_id"] == thread
    rpc.ignore_pause = False
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused" and not store.role_state()["goal_owned"]
    action(store, "bind", payload=replacement)
    assert store.role_state()["thread_id"] == replacement["thread_id"]


def test_bind_during_goal_activation_waits_for_compensating_stop_readback(store):
    replacement = {"thread_id": str(uuid4())}

    class StopDuringActivation(FakeRPC):
        ignore_pause = True

        async def request(self, method, params):
            if method == "thread/goal/set" and params.get("status") == "paused" and self.ignore_pause:
                self.calls.append((method, params))
                return {}
            result = await super().request(method, params)
            if method == "thread/goal/set" and params.get("status") == "active":
                action(store, "stop")
                with pytest.raises(RoleError, match="role_bind_requires_idle"):
                    action(store, "bind", payload=replacement)
            return result

    action(store, "start")
    rpc = StopDuringActivation()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": "paused"}
    run = executor(store, rpc)
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "active" and store.role_state()["goal_owned"]
    with pytest.raises(RoleError, match="role_bind_requires_idle"):
        action(store, "bind", payload=replacement)
    rpc.ignore_pause = False
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused" and not store.role_state()["goal_owned"]
    action(store, "bind", payload=replacement)
    assert store.role_state()["thread_id"] == replacement["thread_id"]


@pytest.mark.parametrize("missing_document", [False, True], ids=["hash-mismatch", "missing-document"])
def test_sleep_resume_validates_bundle_before_native_goal_activation(store, missing_document):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    action(store, "sleep", payload={"seconds": 3600}, actor="M1")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    store.runtime_update("M1", next_wake_at="2020-01-01T00:00:00+00:00")
    document = Path(store.role_state()["instruction_root"]) / "docs/agent/modules/F4.md"
    if missing_document:
        document.unlink()
    else:
        document.write_text("Changed policy under unchanged revision\n")
    before = len(rpc.calls)
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["last_error"].startswith("role_instruction_")
    assert not any(
        method in {"thread/inject_items", "turn/start", "turn/steer"}
        or (method == "thread/goal/set" and params.get("status") == "active")
        for method, params in rpc.calls[before:]
    )
    action(store, "stop")
    asyncio.run(run.tick())
    assert not store.role_state()["goal_owned"]
    assert store.role_state()["last_error"].startswith("role_instruction_")


def test_sleep_resume_checks_explicit_owner_bundle_before_goal_activation(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    action(store, "sleep", payload={"seconds": 3600}, actor="M1")
    asyncio.run(run.tick())
    store.runtime_update("M1", next_wake_at="2020-01-01T00:00:00+00:00")
    root = Path(store.role_state()["instruction_root"])
    store.submit_owner_event(
        "missing-owner-policy", instruction_root=str(root / "missing"), instruction_revision="b" * 40
    )
    before = len(rpc.calls)
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["last_error"].startswith("role_instruction_")
    assert store.owner_event_receipt("missing-owner-policy")["status"] == "queued"
    assert not any(
        method in {"thread/inject_items", "turn/start", "turn/steer"}
        or (method == "thread/goal/set" and params.get("status") == "active")
        for method, params in rpc.calls[before:]
    )


def _current_role_bundles(store, tmp_path, monkeypatch):
    old_root = Path(store.role_state()["instruction_root"])
    new_root = create_instruction_bundle(tmp_path / "new-snapshot", "b" * 40)
    marker = "POLICY_B: all CRM writes are forbidden; read-only analysis is allowed."
    document = new_root / "docs/agent/modules/M1.md"
    document.write_text(document.read_text() + "\n" + marker + "\n")
    seal_instruction_bundle(new_root)
    assert load_instruction_bundle(old_root, "a" * 40).sha256 != load_instruction_bundle(new_root, "b" * 40).sha256
    current = [old_root]
    resolve = Path.resolve

    def synthetic_current(path, *args, **kwargs):
        if str(path) == "/opt/autostop-manager-releases/current":
            return current[0]
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", synthetic_current)
    thread = store.role_state()["thread_id"]
    action(store, "bind", payload={"thread_id": thread})
    return current, new_root, marker, thread


def _stopped_role_with_new_policy(store, rpc, tmp_path, monkeypatch):
    current, new_root, marker, thread = _current_role_bundles(store, tmp_path, monkeypatch)
    run = executor(store, rpc)
    action(store, "start")
    asyncio.run(run.tick())
    action(store, "stop")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused" and not store.role_state()["goal_owned"]
    assert not store.pending_dispatches("M1")
    current[0] = new_root
    return run, thread, marker


def test_restart_delivers_new_policy_before_native_goal_activation(store, tmp_path, monkeypatch):
    class EagerNativeGoal(FakeRPC):
        eager = False
        policy_seen_before_activation = False
        marker = ""

        async def request(self, method, params):
            result = await super().request(method, params)
            if self.eager and method == "thread/goal/set" and params["status"] == "active":
                thread = params["threadId"]
                self.policy_seen_before_activation = any(
                    self.marker in item["content"][0]["text"] for item in self.injected.get(thread, [])
                )
                if not any(turn["status"] == "inProgress" for turn in self.turns[thread]):
                    self.turns[thread].insert(0, {"id": "native-continuation", "status": "inProgress", "items": []})
            return result

    rpc = EagerNativeGoal()
    run, thread, marker = _stopped_role_with_new_policy(store, rpc, tmp_path, monkeypatch)
    rpc.eager = True
    rpc.marker = marker
    before = len(rpc.calls)
    action(store, "start")
    asyncio.run(run.tick())
    calls = rpc.calls[before:]
    injection = next(i for i, (method, _) in enumerate(calls) if method == "thread/inject_items")
    start = next(i for i, (method, _) in enumerate(calls) if method == "turn/start")
    activation = next(
        i for i, (method, params) in enumerate(calls) if method == "thread/goal/set" and params["status"] == "active"
    )
    assert injection < start < activation
    assert rpc.policy_seen_before_activation and rpc.goals[thread]["status"] == "active"
    assert marker in rpc.injected[thread][-1]["content"][0]["text"]
    assert rpc.thread_configs[thread] == {"project_doc_max_bytes": 0, "skills.include_instructions": False}
    assert not any(turn["id"] == "native-continuation" for turn in rpc.turns[thread])


@pytest.mark.parametrize(
    "injection_error",
    [RoleRpcRejected("role_rpc_rejected"), TimeoutError("synthetic lost policy ack")],
    ids=["rejected", "unknown"],
)
def test_failed_new_policy_keeps_existing_goal_paused_without_blind_replay(
    store, tmp_path, monkeypatch, injection_error
):
    rpc = FakeRPC()
    run, thread, _ = _stopped_role_with_new_policy(store, rpc, tmp_path, monkeypatch)
    rpc.injection_error = injection_error
    before = len(rpc.calls)
    action(store, "start")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["last_error"].startswith("role_instruction_injection_")
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    calls = rpc.calls[before:]
    assert sum(method == "thread/inject_items" for method, _ in calls) == 1
    assert not any(
        method in {"turn/start", "turn/steer"} or (method == "thread/goal/set" and params.get("status") == "active")
        for method, params in calls
    )


def test_pending_new_policy_does_not_rewrite_or_reactivate_inflight_native_turn(store, tmp_path, monkeypatch):
    current, new_root, _, thread = _current_role_bundles(store, tmp_path, monkeypatch)
    rpc = FakeRPC()
    run = executor(store, rpc)
    action(store, "start")
    asyncio.run(run.tick())
    rpc.turns[thread][0]["status"] = "completed"
    native = {"id": "native-continuation", "status": "inProgress", "items": []}
    rpc.turns[thread].insert(0, native)
    action(store, "sleep", payload={"seconds": 3600}, actor="M1")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    current[0] = new_root
    store.runtime_update("M1", next_wake_at="2020-01-01T00:00:00+00:00")
    before = len(rpc.calls)
    asyncio.run(run.tick())
    assert native["status"] == "inProgress" and rpc.goals[thread]["status"] == "paused"
    assert not any(
        method in {"thread/inject_items", "turn/start", "turn/steer"}
        or (method == "thread/goal/set" and params.get("status") == "active")
        for method, params in rpc.calls[before:]
    )


def test_restart_reconciles_existing_duty_without_second_launch(store):
    action(store, "start")
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    recovered = RoleStore(store.path)
    recovered.initialize()
    asyncio.run(executor(recovered, rpc).tick())
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1
    assert recovered.role_state()["duty_active"]


def test_unmatched_unknown_launch_holds_and_never_blindly_retries(store):
    rpc = FakeRPC()
    rpc.lose_start = True
    store.submit_owner_event("event-unmatched")
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["items"] = []
    asyncio.run(run.tick())
    asyncio.run(run.tick())
    assert store.owner_event_receipt("event-unmatched")["status"] == "unknown"
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1


def test_manually_paused_duty_still_accepts_owner_one_shot_without_resuming(store):
    action(store, "start")
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": "paused"}
    store.runtime_update("M1", goal_owned=True, goal_applied="active")
    store.submit_owner_event("manual-pause-question")
    asyncio.run(executor(store, rpc).tick())
    assert store.owner_event_receipt("manual-pause-question")["status"] == "running"
    assert rpc.goals[thread]["status"] == "paused"
    assert not any(method == "thread/goal/set" for method, _ in rpc.calls)


def make_task(store, resource, *, actor="M1", urgent=False):
    return action(
        store,
        "task_create",
        payload={
            "title": "Synthetic task",
            "objective": "Synthetic correction",
            "resource": resource,
            "acceptance": "Focused regression passes",
            "urgent": urgent,
        },
        actor=actor,
    )["item"]


def test_task_priority_review_rework_and_release_state_are_separate(store):
    low = make_task(store, "manager:low", actor="M2", urgent=True)
    high = make_task(store, "manager:high", actor="owner")
    with pytest.raises(RoleError, match="higher_priority"):
        action(store, "task_claim", "M2", {"id": low["id"]}, actor="M2", revision=1)
    claimed = action(store, "task_claim", "M2", {"id": high["id"]}, actor="M2", revision=1)["item"]
    result = action(
        store,
        "task_result",
        "M2",
        {
            "id": high["id"],
            "result": "Patch ready",
            "verification": "Regression passes",
            "release_required": True,
            "source_state": "published",
            "deployment_state": "pending",
        },
        actor="M2",
        revision=claimed["revision"],
    )["item"]
    assert result["data"]["source_state"] == "published"
    assert result["data"]["deployment_state"] == "pending"
    returned = action(
        store,
        "task_return",
        payload={"id": high["id"], "reason": "Add regression for empty input"},
        actor="M1",
        revision=result["revision"],
    )["item"]
    assert returned["status"] == "queued"
    assert returned["data"]["claimed_by"] is None
    assert store.read("tasks", "M2", limit=1)["items"][0]["id"] == high["id"]
    assert (
        action(store, "task_claim", "M2", {"id": high["id"]}, actor="M2", revision=returned["revision"])["item"][
            "status"
        ]
        == "running"
    )


def test_task_claim_race_and_same_key_submission_are_serialized(store):
    from concurrent.futures import ThreadPoolExecutor

    first = make_task(store, "manager:a")
    second = make_task(store, "manager:b")

    def claim(task):
        try:
            return action(store, "task_claim", "M2", {"id": task["id"]}, actor="M2", revision=1)["item"]["status"]
        except RoleError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [first, second]))
    assert sorted(results) == ["role_engineer_busy", "running"]
    payload = {"title": "Exactly once", "objective": "Check", "resource": "manager:once", "acceptance": "Pass"}
    a = action(store, "task_create", payload=payload, key="exact-task", revision=1)
    assert action(store, "task_create", payload=payload, key="exact-task", revision=1) == a


def test_older_open_questions_and_approvals_are_not_hidden_by_closed_items(store):
    question = action(store, "question_create", payload={"text": "Waiting for owner"})["item"]
    closed = action(store, "question_create", payload={"text": "Resolved later"})["item"]
    action(
        store,
        "question_resolve",
        payload={"id": closed["id"], "answer": "Done", "answer_ref": "test:answer"},
        revision=1,
    )
    assert store.read("questions", limit=1)["items"][0]["id"] == question["id"]
    proposal = {
        "action": "deploy",
        "target": "manager",
        "target_revision": "old",
        "proposal_ref": "git:new",
        "proposal_hash": "b" * 64,
    }
    waiting = action(store, "approval_create", payload=proposal)["item"]
    finished = action(store, "approval_create", payload=proposal)["item"]
    action(
        store,
        "approval_decide",
        payload={"id": finished["id"], "decision": "rejected", "decision_ref": "test:no"},
        revision=1,
    )
    assert store.read("approvals", limit=1)["items"][0]["id"] == waiting["id"]


def test_task_notice_wakes_m2_and_does_not_claim_work(store):
    task = make_task(store, "manager:notice")
    action(store, "start", "M2")
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    assert store.read("tasks", "M2", reference_id=task["id"])["items"][0]["status"] == "queued"
    starts = [params for method, params in rpc.calls if method == "turn/start"]
    assert len(starts) == 1
    assert task["id"] in starts[0]["input"][0]["text"]


def test_owner_receipt_keeps_original_pinned_snapshot(store):
    state = store.role_state()
    receipt = store.submit_owner_event(
        "original-instructions",
        instruction_root=state["instruction_root"],
        instruction_revision=state["instruction_revision"],
    )
    assert receipt["instruction_root"] == state["instruction_root"]
    assert receipt["instruction_revision"] == state["instruction_revision"]
    with pytest.raises(RoleError, match="owner_event_conflict"):
        store.submit_owner_event("original-instructions", instruction_root="/different", instruction_revision="b" * 40)


def test_maintenance_hold_does_not_start_a_turn(store):
    automation = AutomationStore(store.path)
    automation.initialize()
    with store.connection(write=True) as db:
        db.execute("UPDATE manager_automation_controller_runtime SET hold_enabled=1 WHERE singleton=1")
    action(store, "start")
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    assert not any(method in {"turn/start", "thread/goal/set"} for method, _ in rpc.calls)


def test_recovery_reads_old_dispatch_beyond_recent_thirty_turns(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    later = [{"id": str(uuid4()), "status": "completed", "items": []} for _ in range(65)]
    later[0]["status"] = "inProgress"
    rpc.turns[thread] = later + rpc.turns[thread]
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert store.pending_dispatches("M1")[0]["scan_cursor"] == "60"
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert not store.pending_dispatches("M1")
    assert store.role_state()["active_dispatch_id"] is None
    assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1


def test_unknown_interrupt_remains_reconcilable_until_independent_readback(store):
    class LoseInterrupt(FakeRPC):
        def __init__(self):
            super().__init__()
            self.lose_interrupt = True

        async def request(self, method, params):
            if method == "turn/interrupt" and self.lose_interrupt:
                self.lose_interrupt = False
                raise TimeoutError("synthetic unknown interrupt")
            return await super().request(method, params)

    action(store, "start")
    rpc = LoseInterrupt()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    active = {"id": str(uuid4()), "status": "inProgress", "items": []}
    rpc.turns[thread].insert(0, active)
    asyncio.run(run.tick())
    action(store, "stop")
    asyncio.run(run.tick())
    assert store.role_state()["goal_owned"]
    assert active["status"] == "inProgress"
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert active["status"] == "interrupted"
    asyncio.run(run.tick())
    assert not store.role_state()["goal_owned"]


def test_role_jobs_visible_in_center_are_source_owned_and_start_off(store):
    from autostop_manager.automation_registry import ROLE_TEMPLATES

    center = AutomationStore(store.path)
    jobs = center.status()["jobs"]
    assert {job["template_id"] for job in jobs} == set(ROLE_TEMPLATES)
    assert all(job["desired_state"] == "off" and job["next_run_at"] is None for job in jobs)
    action(store, "start")
    action(store, "sleep", payload={"seconds": 3600}, actor="M1")
    before = store.role_state()["generation"]
    store.project_automation_jobs()
    store.project_automation_jobs()
    assert store.role_state()["generation"] == before
    jobs = {j["source_role"]: j for j in center.status()["jobs"]}
    assert jobs["M1"]["desired_state"] == "on"
    assert jobs["M1"]["next_run_at"]
    assert jobs["M2"]["desired_state"] == "off"
    assert jobs["M1"]["control_mode"] == "read_only"
    service = AutomationControlService(center)
    with pytest.raises(AutomationError, match="automation_role_control_required"):
        service.handle(
            {
                "protocol": AUTOMATION_CONTROL_PROTOCOL,
                "request_id": str(uuid4()),
                "operation": "set_enabled",
                "actor": {"kind": "codex", "id": "test", "is_admin": True},
                "payload": {"job_id": jobs["M1"]["job_id"], "enabled": False},
                "idempotency_key": "source-owned-role",
                "expected_revision": jobs["M1"]["revision"],
            }
        )
    assert center.preview(
        {"target_operation": "set_enabled", "target_payload": {"job_id": jobs["M1"]["job_id"], "enabled": False}}
    )["locked"]
    assert store.role_state()["duty_active"]
    action(store, "stop")
    assert center.status(job_id=jobs["M1"]["job_id"])["jobs"][0]["desired_state"] == "off"


def test_automation_role_adapter_only_requests_short_reconciliation(store):
    from autostop_manager.automation_daemon import AutomationDaemon

    rpc = FakeRPC()
    run = executor(store, rpc)
    daemon = AutomationDaemon(store=AutomationStore(store.path), role_executor=run)
    assert {"m1_duty_v1", "m2_duty_v1"} <= daemon.executors.keys()
    result = asyncio.run(daemon.executors["m1_duty_v1"]({"template_id": "m1_duty_v1"}))
    assert result["ok"] and run.wake_event.is_set()
    assert not rpc.calls
    assert not store.role_state()["duty_active"]


def test_exact_approval_is_consumed_once_even_when_result_response_is_replayed(store):
    proposal = {
        "action": "message",
        "target": "employee:test",
        "target_revision": "v1",
        "proposal_ref": "draft:1",
        "proposal_hash": "c" * 64,
    }
    item = action(store, "approval_create", payload=proposal)["item"]
    approved = action(
        store,
        "approval_decide",
        payload={"id": item["id"], "decision": "approved", "decision_ref": "owner:event"},
        revision=1,
    )["item"]
    payload = {**proposal, "id": item["id"]}
    first = action(store, "approval_consume", payload=payload, revision=approved["revision"], key="exact-approval")
    assert (
        action(store, "approval_consume", payload=payload, revision=approved["revision"], key="exact-approval") == first
    )
    with pytest.raises(RoleError, match="approval_not_available"):
        action(store, "approval_consume", payload=payload, revision=first["item"]["revision"])


def test_codex_rpc_ignores_notifications_and_reports_fixed_errors():
    import json
    from autostop_manager.role_executor import CodexRoleRPC, RoleRpcRejected

    class FakeSocket:
        def __init__(self, frames):
            self.frames = frames
            self.sent = []

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def recv(self):
            return json.dumps(self.frames.pop(0))

    frames = [{"method": "notification", "id": 1}, {"id": 99, "result": {}}, {"id": 1, "result": {"ready": True}}]
    socket = FakeSocket(frames)
    assert asyncio.run(CodexRoleRPC(socket).request("test", {})) == {"ready": True}
    assert socket.sent[0]["method"] == "test"
    with pytest.raises(RoleRpcRejected, match=r"^role_rpc_rejected$"):
        asyncio.run(
            CodexRoleRPC(FakeSocket([{"id": 1, "error": {"message": "private synthetic detail"}}])).request("test", {})
        )
    with pytest.raises(RoleError, match="response_invalid"):
        asyncio.run(CodexRoleRPC(FakeSocket([{"id": 1, "result": []}])).request("test", {}))


def test_rpc_connection_initializes_without_touching_live_socket(monkeypatch):
    import json
    import websockets.asyncio.client
    from autostop_manager.role_executor import role_rpc

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

        async def recv(self):
            return json.dumps({"id": 1, "result": {}})

    socket = FakeSocket()

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield socket

    monkeypatch.setattr(websockets.asyncio.client, "unix_connect", connect)

    async def run():
        async with role_rpc() as rpc:
            assert rpc.websocket is socket

    asyncio.run(run())
    assert [message["method"] for message in socket.sent] == ["initialize", "initialized"]


def test_foreign_goal_and_wrong_model_require_binding_correction(store):
    action(store, "start")
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": "Another task", "status": "active"}
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"] == "role_foreign_goal"
    assert not any(m == "turn/start" for m, _ in rpc.calls)
    previous = len(rpc.calls)
    asyncio.run(executor(store, rpc).tick())
    assert len(rpc.calls) == previous
    action(store, "start")
    rpc.goals.clear()
    original = rpc.request

    async def wrong_model(method, params):
        result = await original(method, params)
        if method == "thread/resume":
            result["model"] = "another-model"
        return result

    rpc.request = wrong_model
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"] == "role_model_mismatch"


@pytest.mark.parametrize("role", ["M1", "M2"])
def test_role_turn_uses_exact_verified_policy_and_preserves_managed_settings(store, role):
    action(store, "start", role)
    rpc = FakeRPC()
    original_selectors = [{"path": f"/synthetic/disabled-{index}", "enabled": False} for index in range(27)]
    rpc.config = {
        "skills": {"config": deepcopy(original_selectors)},
        "developer_instructions": "Existing custom developer instructions",
        "approval_policy": "on-request",
    }
    thread = store.role_state(role)["thread_id"]
    thread_selectors = [{"path": "/thread-only/disabled-skill", "enabled": False}]
    rpc.thread_configs[thread] = {"skills": {"config": deepcopy(thread_selectors), "max_context_tokens": 123}}
    asyncio.run(executor(store, rpc).tick())
    state = store.role_state(role)
    bundle = load_instruction_bundle(state["instruction_root"], state["instruction_revision"])
    context = rpc.injected[state["thread_id"]]
    assert len(context) == 1 and context[0]["role"] == "developer"
    text = context[0]["content"][0]["text"]
    assert bundle.root in text and bundle.sha256 in text
    assert f'"profile": "{role}"' in text
    assert bundle.files[f"docs/agent/modules/{role}.md"].decode() in text
    methods = [method for method, _params in rpc.calls]
    assert methods.index("thread/inject_items") < methods.index("turn/start")
    profiles = [params["config"] for method, params in rpc.calls if method == "thread/resume" and "config" in params]
    assert len(profiles) == 1
    assert profiles[0]["project_doc_max_bytes"] == 0
    assert profiles[0]["skills.include_instructions"] is False
    assert "skills" not in profiles[0] and "skills.config" not in profiles[0]
    assert rpc.thread_configs[thread]["skills"]["config"] == thread_selectors
    assert rpc.thread_configs[thread]["skills"]["max_context_tokens"] == 123
    assert rpc.config["skills"]["config"] == original_selectors
    assert not any(
        key in params for _method, params in rpc.calls for key in ("baseInstructions", "developerInstructions")
    )


def test_role_owner_turn_uses_original_pin_without_switching_to_bound_bundle(store, tmp_path):
    original = create_instruction_bundle(tmp_path / "earlier-snapshot", "b" * 40)
    store.submit_owner_event("old-pinned-owner", instruction_root=str(original), instruction_revision="b" * 40)
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    context = rpc.injected[store.role_state()["thread_id"]][0]["content"][0]["text"]
    assert str(original) in context
    assert store.role_state()["instruction_root"] not in context
    assert store.owner_event_receipt("old-pinned-owner")["status"] == "running"


def test_active_role_defers_different_bundle_without_injection_or_steer(store, tmp_path):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    other = create_instruction_bundle(tmp_path / "other-snapshot", "b" * 40)
    store.submit_owner_event("different-owner-pin", instruction_root=str(other), instruction_revision="b" * 40)
    asyncio.run(run.tick())
    assert store.owner_event_receipt("different-owner-pin")["status"] == "queued"
    assert len([method for method, _ in rpc.calls if method == "thread/inject_items"]) == 1
    assert not any(method == "turn/steer" for method, _ in rpc.calls)
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert store.owner_event_receipt("different-owner-pin")["status"] == "running"
    assert len(rpc.injected[thread]) == 2
    assert str(other) in rpc.injected[thread][-1]["content"][0]["text"]


def test_active_role_without_explicit_origin_pin_defers_after_current_link_changes(store, tmp_path, monkeypatch):
    import autostop_manager.role_executor as role_executor

    first = Path(store.role_state()["instruction_root"])
    second = create_instruction_bundle(tmp_path / "next-current", "b" * 40)
    selected = [first]

    def instruction_path(value):
        return selected[0] if value == "/opt/autostop-manager-releases/current" else Path(value)

    monkeypatch.setattr(role_executor, "Path", instruction_path)
    action(store, "bind", payload={"thread_id": store.role_state()["thread_id"]})
    assert store.role_state()["instruction_root"] is None
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    assert str(first) in rpc.injected[thread][0]["content"][0]["text"]
    selected[0] = second
    store.submit_owner_event("new-current-owner", instruction_root=str(second), instruction_revision="b" * 40)
    asyncio.run(run.tick())
    assert store.owner_event_receipt("new-current-owner")["status"] == "queued"
    assert len(rpc.injected[thread]) == 1
    assert not any(method == "turn/steer" for method, _ in rpc.calls)
    assert len([method for method, _ in rpc.calls if method == "turn/start"]) == 1
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert store.owner_event_receipt("new-current-owner")["status"] == "running"
    assert str(second) in rpc.injected[thread][-1]["content"][0]["text"]


@pytest.mark.parametrize("failure", ["rejected", "unknown"])
def test_role_injection_failure_is_durable_hold_without_turn_or_replay(store, failure):
    store.submit_owner_event("failed-policy-append")
    rpc = FakeRPC()
    rpc.injection_error = (
        RoleRpcRejected("private rejected payload") if failure == "rejected" else TimeoutError("private lost payload")
    )
    asyncio.run(executor(store, rpc).tick())
    code = "role_instruction_injection_" + failure
    assert store.role_state()["last_error"] == code
    assert store.role_state()["state"] == "attention"
    assert store.owner_event_receipt("failed-policy-append")["status"] == "failed"
    assert not any(method in {"turn/start", "turn/steer"} for method, _ in rpc.calls)
    calls = len(rpc.calls)
    # A fresh controller and storage object cannot turn the unknown append into a retry.
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert len(rpc.calls) == calls
    assert len([method for method, _ in rpc.calls if method == "thread/inject_items"]) == 1


def test_role_injection_failure_pauses_only_its_owned_native_goal(store):
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    store.submit_owner_event("pause-after-policy-failure")
    rpc.injection_error = TimeoutError("private payload")
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["duty_active"]
    assert store.role_state()["goal_applied"] == "paused"
    assert store.role_state()["last_error"] == "role_instruction_injection_unknown"


@pytest.mark.parametrize(
    "transport_error",
    [ConnectionClosed(None, None), RuntimeError("synthetic private transport payload")],
    ids=["websocket-closed", "runtime-error"],
)
def test_instruction_hold_survives_transport_failure_during_readback(store, transport_error):
    class ReadbackFault(FakeRPC):
        fail_readback = False

        async def request(self, method, params):
            if method == "thread/inject_items" and self.injection_error is not None:
                self.fail_readback = True
            if method == "thread/goal/get" and self.fail_readback:
                self.fail_readback = False
                self.calls.append((method, params))
                raise transport_error
            return await super().request(method, params)

    action(store, "start")
    rpc = ReadbackFault()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    store.submit_owner_event("transport-after-unknown-append")
    rpc.injection_error = TimeoutError("synthetic lost append")
    asyncio.run(run.tick())
    code = "role_instruction_injection_unknown"
    assert store.role_state()["last_error"] == code
    assert rpc.goals[thread]["status"] == "active"
    generation_methods = {"thread/inject_items", "turn/start", "turn/steer"}
    generation_calls = [call for call in rpc.calls if call[0] in generation_methods]
    assert [method for method, _ in generation_calls] == ["thread/inject_items", "turn/start", "thread/inject_items"]
    # The persisted hold also survives a later reconnect/readback failure.
    rpc.fail_readback = True
    asyncio.run(run.tick())
    assert store.role_state()["last_error"] == code
    assert [call for call in rpc.calls if call[0] in generation_methods] == generation_calls
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["goal_applied"] == "paused"
    assert store.role_state()["last_error"] == code
    assert [call for call in rpc.calls if call[0] in generation_methods] == generation_calls


@pytest.mark.parametrize("failure_stage", ["open", "exit"])
@pytest.mark.parametrize(
    "transport_error",
    [ConnectionClosed(None, None), RuntimeError("synthetic private connection payload")],
    ids=["websocket-closed", "runtime-error"],
)
def test_instruction_hold_survives_rpc_factory_failure_for_each_role(store, failure_stage, transport_error):
    rpc = FakeRPC()
    codes = {"M1": "role_instruction_injection_unknown", "M2": "role_instruction_injection_rejected"}
    for role, code in codes.items():
        action(store, "start", role)
        thread = store.role_state(role)["thread_id"]
        rpc.goals[thread] = {"objective": role_objective(role), "status": "active"}
        store.runtime_update(role, goal_owned=True, goal_applied="active", state="attention", last_error=code)

    @asynccontextmanager
    async def failing_factory():
        if failure_stage == "open":
            raise transport_error
        yield rpc
        raise transport_error

    asyncio.run(RoleExecutor(store, rpc_factory=failing_factory).tick())
    assert {role: store.role_state(role)["last_error"] for role in codes} == codes
    asyncio.run(executor(store, rpc).tick())
    for role, code in codes.items():
        state = store.role_state(role)
        assert state["last_error"] == code
        assert state["goal_applied"] == "paused"
        assert rpc.goals[state["thread_id"]]["status"] == "paused"
    assert not any(method in {"thread/inject_items", "turn/start", "turn/steer"} for method, _ in rpc.calls)


@pytest.mark.parametrize("pause_failure,stop", [("rejected", True), ("lost", True), ("lost", False)])
def test_instruction_hold_reconciles_owned_goal_without_policy_replay(store, pause_failure, stop):
    class PauseFault(FakeRPC):
        pause_attempts = 0
        fail_pause = True

        async def request(self, method, params):
            if method == "thread/goal/set" and params.get("status") == "paused":
                self.pause_attempts += 1
                if self.fail_pause:
                    self.fail_pause = False
                    if pause_failure == "rejected":
                        raise RoleRpcRejected("synthetic rejected pause")
                    await super().request(method, params)
                    raise TimeoutError("synthetic lost pause response")
            return await super().request(method, params)

    action(store, "start")
    rpc = PauseFault()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    rpc.turns[thread][0]["status"] = "completed"
    store.submit_owner_event("held-owner-policy")
    rpc.injection_error = TimeoutError("synthetic lost append")
    asyncio.run(run.tick())
    code = "role_instruction_injection_unknown"
    assert store.role_state()["last_error"] == code
    assert store.role_state()["goal_applied"] == "active"
    assert rpc.pause_attempts == 1
    if pause_failure == "rejected":
        # Keep observing the exact goal after a known refusal; do not blindly
        # repeat that write before the owner's new stop revision.
        asyncio.run(run.tick())
        assert rpc.pause_attempts == 1
        assert rpc.goals[thread]["status"] == "active"
        continuation = {"id": "owned-native-continuation", "status": "inProgress", "items": []}
        rpc.turns[thread].insert(0, continuation)
    if stop:
        action(store, "stop")
    # Exercise persistence on the unknown-response path, without resending its
    # already-applied pause or any policy message.
    if pause_failure == "lost":
        run = executor(RoleStore(store.path), rpc)
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert store.role_state()["goal_applied"] == "paused"
    assert store.role_state()["last_error"] == code
    assert store.role_state()["duty_active"] is not stop
    assert store.role_state()["goal_owned"] is not stop
    assert rpc.pause_attempts == (2 if pause_failure == "rejected" else 1)
    assert len([method for method, _ in rpc.calls if method == "turn/start"]) == 1
    assert len([method for method, _ in rpc.calls if method == "thread/inject_items"]) == 2
    assert not any(method == "turn/steer" for method, _ in rpc.calls)
    if pause_failure == "rejected":
        assert continuation["status"] == "interrupted"
    asyncio.run(run.tick())
    assert rpc.goals[thread]["status"] == "paused"
    assert len([method for method, _ in rpc.calls if method == "thread/inject_items"]) == 2


def test_instruction_hold_does_not_pause_or_interrupt_a_foreign_goal(store):
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": "Unrelated user goal", "status": "active"}
    rpc.turns[thread] = [{"id": "foreign-turn", "status": "inProgress", "items": []}]
    store.runtime_update(
        "M1", goal_owned=True, goal_applied="active", state="attention", last_error="role_instruction_injection_unknown"
    )
    asyncio.run(executor(store, rpc).tick())
    assert rpc.goals[thread]["status"] == "active"
    assert rpc.turns[thread][0]["status"] == "inProgress"
    assert store.role_state()["last_error"] == "role_instruction_injection_unknown"
    assert not any(
        method in {"thread/goal/set", "turn/interrupt", "thread/inject_items", "turn/start", "turn/steer"}
        for method, _ in rpc.calls
    )


def test_instruction_hold_requires_pause_readback_before_marking_applied(store):
    class IgnoredPause(FakeRPC):
        async def request(self, method, params):
            if method == "thread/goal/set" and params.get("status") == "paused":
                self.calls.append((method, params))
                return {}
            return await super().request(method, params)

    rpc = IgnoredPause()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": "active"}
    store.runtime_update(
        "M1", goal_owned=True, goal_applied="active", state="attention", last_error="role_instruction_injection_unknown"
    )
    asyncio.run(executor(store, rpc).tick())
    assert rpc.goals[thread]["status"] == "active"
    assert store.role_state()["goal_applied"] == "active"
    assert len([method for method, _ in rpc.calls if method == "thread/goal/get"]) == 2
    assert not any(method in {"thread/inject_items", "turn/start", "turn/steer"} for method, _ in rpc.calls)


def test_instruction_hold_does_not_claim_stopped_while_goal_is_missing_and_turn_is_active(store):
    rpc = FakeRPC()
    thread = store.role_state()["thread_id"]
    rpc.turns[thread] = [{"id": "unproven-active-turn", "status": "inProgress", "items": []}]
    store.runtime_update(
        "M1", goal_owned=True, goal_applied="active", state="attention", last_error="role_instruction_injection_unknown"
    )
    run = executor(store, rpc)
    asyncio.run(run.tick())
    assert store.role_state()["goal_owned"] is True
    assert store.role_state()["goal_applied"] == "active"
    assert not any(method in {"thread/goal/set", "turn/interrupt"} for method, _ in rpc.calls)
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert store.role_state()["goal_owned"] is False
    assert store.role_state()["goal_applied"] == "paused"
    assert store.role_state()["last_error"] == "role_instruction_injection_unknown"


def test_instruction_hold_does_not_override_new_explicit_recovery(store):
    class RecoverDuringRead(FakeRPC):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "thread/goal/get":
                action(store, "start")
            return result

    rpc = RecoverDuringRead()
    thread = store.role_state()["thread_id"]
    rpc.goals[thread] = {"objective": role_objective("M1"), "status": "active"}
    store.runtime_update(
        "M1", goal_owned=True, goal_applied="active", state="attention", last_error="role_instruction_injection_unknown"
    )
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"] is None
    assert rpc.goals[thread]["status"] == "active"
    assert not any(
        method in {"thread/goal/set", "turn/interrupt", "thread/inject_items", "turn/start", "turn/steer"}
        for method, _ in rpc.calls
    )


@pytest.mark.parametrize("bad_pin", [False, True])
def test_role_rejects_tampered_or_explicitly_missing_bundle_before_append(store, bad_pin):
    state = store.role_state()
    root = Path(state["instruction_root"])
    if bad_pin:
        store.submit_owner_event("invalid-pin", instruction_root=str(root / "missing"), instruction_revision="b" * 40)
    else:
        (root / "docs/agent/modules/F4.md").write_text("Altered pricing under unchanged REVISION\n")
        action(store, "start")
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"].startswith("role_instruction_")
    assert not any(method in {"thread/inject_items", "turn/start", "turn/steer"} for method, _ in rpc.calls)


def test_owner_stop_after_policy_append_prevents_role_turn_start(store):
    store.submit_owner_event("stopped-after-policy-append")

    class StopAfterAppend(FakeRPC):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "thread/inject_items":
                action(store, "stop")
            return result

    rpc = StopAfterAppend()
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"] == "role_instruction_context_changed"
    assert not any(method == "turn/start" for method, _ in rpc.calls)


def test_role_becoming_active_during_preparation_never_receives_injection(store):
    store.submit_owner_event("manual-turn-before-append")

    class ActiveBeforeAppend(FakeRPC):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == "thread/read":
                result["thread"]["status"] = {"type": "active"}
            return result

    rpc = ActiveBeforeAppend()
    asyncio.run(executor(store, rpc).tick())
    assert store.role_state()["last_error"] == "role_instruction_thread_changed"
    assert not any(method in {"thread/inject_items", "turn/start"} for method, _ in rpc.calls)
