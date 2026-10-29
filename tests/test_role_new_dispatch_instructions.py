"""A new dispatch selects current policy; active turns retain their own pin."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from autostop_manager.role_coordination import RoleStore
from tests.instruction_bundle_fakes import create_instruction_bundle
from tests.test_role_coordination import FakeRPC, action, executor
from tests.test_role_coordination import store as store


@pytest.fixture
def current_bundle(store, tmp_path, monkeypatch):
    current = [Path(store.role_state()["instruction_root"])]
    updated = create_instruction_bundle(tmp_path / "current-b", "b" * 40)
    resolve = Path.resolve

    def synthetic_current(path, *args, **kwargs):
        if str(path) == "/opt/autostop-manager-releases/current":
            return current[0]
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", synthetic_current)
    return current, updated


def policy(rpc, thread):
    return rpc.injected[thread][-1]["content"][0]["text"]


def test_new_duty_refreshes_old_binding_before_start_and_retains_actual_active_pin(store, current_bundle):
    original = store.role_state()["instruction_root"]
    current, updated = current_bundle
    current[0] = updated
    action(store, "start")
    before = store.role_state()
    thread = before["thread_id"]
    rpc = FakeRPC()
    private = {"skills": {"config": [{"path": "/thread-only/disabled", "enabled": False}]}}
    rpc.thread_configs[thread] = deepcopy(private)
    rpc.thread_configs[thread]["developer_instructions"] = "Individual guidance stays unchanged."
    disk_config = deepcopy(rpc.config)
    run = executor(store, rpc)
    asyncio.run(run.tick())
    state = store.role_state()
    assert (state["instruction_root"], state["instruction_revision"]) == (str(updated), "b" * 40)
    assert (state["revision"], state["generation"]) == (before["revision"], before["generation"])
    assert str(updated) in policy(rpc, thread) and original not in policy(rpc, thread)
    methods = [method for method, _ in rpc.calls]
    assert methods.index("thread/inject_items") < methods.index("turn/start")
    assert rpc.thread_configs[thread]["skills"] == private["skills"] and rpc.config == disk_config
    assert rpc.thread_configs[thread]["developer_instructions"] == "Individual guidance stays unchanged."
    assert not any(key in params for _, params in rpc.calls for key in ("baseInstructions", "developerInstructions"))
    store.submit_owner_event("old-request", instruction_root=original, instruction_revision="a" * 40)
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    assert store.owner_event_receipt("old-request")["status"] == "queued"
    assert not any(method == "turn/steer" for method, _ in rpc.calls)
    assert len(rpc.injected[thread]) == 1


def test_active_duty_keeps_original_policy_until_its_next_dispatch(store, current_bundle):
    current, updated = current_bundle
    original = current[0]
    action(store, "start")
    rpc = FakeRPC()
    run = executor(store, rpc)
    asyncio.run(run.tick())
    thread = store.role_state()["thread_id"]
    first_policy = deepcopy(rpc.injected[thread][0])
    current[0] = updated
    store.submit_owner_event("legacy-unpinned-request")
    asyncio.run(executor(RoleStore(store.path), rpc).tick())
    steer = [params for method, params in rpc.calls if method == "turn/steer"]
    assert len(steer) == 1 and str(original) in steer[0]["input"][0]["text"]
    assert str(updated) not in steer[0]["input"][0]["text"]
    assert store.role_state()["instruction_root"] == str(original)
    assert rpc.injected[thread] == [first_policy]
    rpc.turns[thread][0]["status"] = "completed"
    asyncio.run(run.tick())
    assert str(updated) in policy(rpc, thread)
    assert store.role_state()["instruction_root"] == str(updated)
    assert rpc.injected[thread][0] == first_policy
    assert len([method for method, _ in rpc.calls if method == "turn/start"]) == 2


def test_explicit_owner_request_keeps_accepted_pin_above_current_bundle(store, current_bundle):
    current, updated = current_bundle
    original = current[0]
    current[0] = updated
    receipt = store.submit_owner_event("pinned-request", instruction_root=str(original), instruction_revision="a" * 40)
    rpc = FakeRPC()
    asyncio.run(executor(store, rpc).tick())
    state = store.role_state()
    assert str(original) in policy(rpc, state["thread_id"]) and str(updated) not in policy(rpc, state["thread_id"])
    after = store.owner_event_receipt("pinned-request")
    assert (after["instruction_root"], after["instruction_revision"]) == (
        receipt["instruction_root"],
        receipt["instruction_revision"],
    )
    assert after["status"] == "running" and not state["duty_active"]
    assert state["instruction_root"] == str(original)
    assert not any(method == "thread/goal/set" for method, _ in rpc.calls)
