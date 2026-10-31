"""Fault contracts for the one-time legacy handoff, using no real services."""

import importlib.util
import os
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "first_transition", Path(__file__).parents[1] / "scripts/crm-first-transition.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeSystem:
    def __init__(self):
        self.current = {
            "id": "a" * 64,
            "state": "running",
            "started": "generation-one",
            "finished": "never",
            "exit": "0",
            "oom": "false",
            "restarts": "0",
            "health": "healthy",
            "revision": m.REVISION,
        }
        self.actions = []
        self.boot_id = "boot-one"
        self.daemon_id = "daemon-one"
        self.blocked = False
        self.stop_error = None

    def crm(self):
        return self.current.copy()

    def boot(self):
        return self.boot_id

    def daemon(self):
        return self.daemon_id

    def preconditions(self):
        return {"synthetic-consumer": "active" if self.blocked else "inactive"}

    def validate(self):
        pass

    def capture(self, initial):
        self.actions.append("capture")
        return {"pid": 7, "start_ticks": 100}

    def freeze(self, value):
        self.actions.append("freeze")

    def gate(self, value):
        self.actions.append("gate")

    def rollback(self, value):
        self.actions.append("rollback")

    def thaw_after_term(self, value):
        self.actions.append("thaw")

    def launch_stop(self, identity):
        self.actions.append("stop")
        if self.stop_error:
            raise self.stop_error
        system = self

        class Process:
            def poll(self):
                return None

            def wait(self):
                system.current.update(state="exited", finished="end-one", health="")
                return 0

        return Process()

    def start(self, identity):
        self.actions.append("start")
        self.current.update(state="running", started="generation-two", health="healthy")

    def healthy(self, identity):
        assert identity == self.current["id"]
        assert self.current["state"] == "running" and self.current["health"] == "healthy"


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(m.common, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(m, "OWNER_UID", os.geteuid())
    monkeypatch.setattr(m, "DEPLOY_LOCK", tmp_path / "deploy.lock")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    system = FakeSystem()
    ident = "b" * 32
    record = {
        **m.plan(system),
        "attempt": ident,
        "phase": "prepared",
        "restore": "pending",
        "result": "pending",
    }
    m.common.save(root / "attempt.json", record)
    return root, ident, system


def test_confirmed_stop_returns_same_container_with_current_data(attempt):
    root, ident, system = attempt
    m.worker(root, ident, system)
    result = m.recover(root, ident, system)
    assert result["phase"] == "complete" and result["restore"] == "ok"
    assert result["result"] == "stop_and_return_verified"
    assert system.actions.count("stop") == system.actions.count("start") == 1
    assert system.actions.index("thaw") < system.actions.index("start")


@pytest.mark.parametrize("phase", ["stop_intent", "stop_running", "stop_unknown"])
def test_unknown_stop_is_not_replayed_or_overtaken(attempt, phase):
    root, ident, system = attempt
    record = m.load(root)
    m.save(root, record, phase=phase)
    with pytest.raises(m.Uncertain):
        m.recover(root, ident, system)
    assert system.actions == []
    assert m.load(root)["restore"] == "unknown"


def test_changed_stopped_receipt_never_starts_another_generation(attempt):
    root, ident, system = attempt
    record = m.load(root)
    system.current.update(state="exited", finished="end-one", health="")
    m.save(root, record, phase="stopped", stop_receipt=system.crm())
    system.current["finished"] = "a-later-stop"
    with pytest.raises(m.Uncertain, match="receipt changed"):
        m.recover(root, ident, system)
    assert system.actions == []


def test_late_worker_cannot_stop_after_recovery(attempt):
    root, ident, system = attempt
    m.recover(root, ident, system)
    m.worker(root, ident, system)
    assert system.actions == []


def test_busy_consumers_refuse_before_capture(attempt):
    root, ident, system = attempt
    system.blocked = True
    m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert system.actions == []
    assert m.load(root)["result"] == "preconditions_changed"


def test_runtime_change_never_replays_old_process_control(attempt):
    root, ident, system = attempt
    record = m.load(root)
    m.save(root, record, phase="stop_unknown")
    system.boot_id = "boot-two"
    system.current["started"] = "generation-two"
    result = m.recover(root, ident, system)
    assert result["restore"] == "ok"
    assert result["result"] == "interrupted_runtime_changed"
    assert system.actions == []


@pytest.mark.parametrize("failed_phase", ["stop_running", "stopped", "start_intent"])
def test_journal_failure_after_stop_request_cannot_strand_crm(attempt, monkeypatch, failed_phase):
    root, ident, system = attempt
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == failed_phase and not injected:
            injected = True
            raise OSError("synthetic durable-write failure")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    assert injected
    assert system.current["state"] == "running" and system.current["started"] == "generation-two"
    assert system.actions.count("start") == 1


def test_commit_intent_write_failure_rolls_back_without_sending_stop(attempt, monkeypatch):
    root, ident, system = attempt
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == "stop_intent" and not injected:
            injected = True
            raise OSError("synthetic durable-write failure before stop")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert injected
    assert "stop" not in system.actions
    assert "rollback" in system.actions
    assert system.current["started"] == "generation-one"


def test_known_stop_launch_failure_returns_precommit_gate(attempt):
    root, ident, system = attempt
    system.stop_error = FileNotFoundError("synthetic CLI unavailable")
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert "rollback" in system.actions and "thaw" not in system.actions
    assert system.actions.count("stop") == 1


def test_recovery_journal_failure_cannot_skip_confirmed_owned_return(attempt, monkeypatch):
    root, ident, system = attempt
    system.current.update(state="exited", finished="end-one", health="")
    m.save(root, m.load(root), phase="stopped", stop_receipt=system.crm())
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == "start_intent" and not injected:
            injected = True
            raise OSError("synthetic recovery journal failure")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.recover(root, ident, system)
    assert injected
    assert system.current["state"] == "running" and system.current["started"] == "generation-two"
    assert system.actions == ["start"]
