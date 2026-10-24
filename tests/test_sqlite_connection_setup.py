from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest

from autostop_manager import automation_registry, storage


class _Connection:
    def __init__(self):
        self.events = []
        self.failure_stage = None
        self.failure = sqlite3.OperationalError("synthetic connection failure")
        self.close_failure = None
        self._row_factory = None

    def _step(self, stage):
        self.events.append(stage)
        if self.failure_stage == stage:
            raise self.failure

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._step("row_factory")
        self._row_factory = value

    def execute(self, statement):
        self._step(statement)

    def commit(self):
        self._step("commit")

    def rollback(self):
        self._step("rollback")

    def close(self):
        self._step("close")
        if self.close_failure is not None:
            raise self.close_failure


@pytest.fixture(params=["automation", "manager"])
def context(request, tmp_path, monkeypatch):
    automation = request.param == "automation"
    module = automation_registry if automation else storage
    store_class = module.AutomationStore if automation else module.StoreState
    path = tmp_path / "synthetic.sqlite3"
    path.touch()
    connection = _Connection()
    state = SimpleNamespace(connection=connection, open_failure=None, open_calls=0, automation=automation)
    row_factory = object()

    def connect(actual_path, **kwargs):
        assert actual_path == path
        assert kwargs == ({"timeout": 5} if automation else {})
        state.open_calls += 1
        if state.open_failure is not None:
            raise state.open_failure
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("explicit connection tests must not resolve a default DB or initialize a schema")

    # Replace only this module's reference, keeping the real sqlite3 module intact.
    monkeypatch.setattr(module, "sqlite3", SimpleNamespace(connect=connect, Row=row_factory))
    monkeypatch.setattr(module, "get_automation_db_path" if automation else "get_db_path", forbidden)
    monkeypatch.setattr(store_class, "initialize", forbidden)
    state.store = store_class(path)
    state.row_factory = row_factory
    return state


def _assert_setup_failure(context):
    connection = context.connection
    with pytest.raises(type(connection.failure)) as failure, context.store.connect():
        pytest.fail("setup failure must prevent entering the connection body")
    assert failure.value is connection.failure
    assert context.open_calls == 1
    assert connection.events.count("close") == 1
    assert "commit" not in connection.events
    assert "rollback" not in connection.events


@pytest.mark.parametrize(
    ("stage", "failure_type"),
    [
        ("row_factory", sqlite3.OperationalError),
        ("PRAGMA foreign_keys = ON", sqlite3.OperationalError),
        ("PRAGMA foreign_keys = ON", KeyboardInterrupt),
    ],
)
def test_setup_failure_closes_without_starting_a_transaction(context, stage, failure_type):
    context.connection.failure_stage = stage
    context.connection.failure = failure_type("synthetic setup failure")
    _assert_setup_failure(context)


@pytest.mark.parametrize("stage", ["chmod", "PRAGMA busy_timeout = 5000"])
@pytest.mark.parametrize("context", ["automation"], indirect=True)
def test_automation_specific_setup_failure_closes(context, monkeypatch, stage):
    context.connection.failure_stage = stage

    def chmod(_path, mode, *, follow_symlinks):
        assert mode == 0o600 and follow_symlinks is False
        context.connection._step("chmod")

    monkeypatch.setattr(automation_registry, "os", SimpleNamespace(geteuid=os.geteuid, chmod=chmod))
    _assert_setup_failure(context)


@pytest.mark.parametrize("outcome", ["success", "body_error", "commit_error", "interrupt"])
def test_connection_transaction_contract_is_preserved(context, outcome):
    connection = context.connection
    failure = KeyboardInterrupt("synthetic interrupt") if outcome == "interrupt" else ValueError("synthetic failure")
    if outcome == "commit_error":
        connection.failure_stage = "commit"
        connection.failure = failure

    def use_connection():
        with context.store.connect() as opened:
            assert opened is connection
            assert opened.row_factory is context.row_factory
            connection.events.append("body")
            if outcome in {"body_error", "interrupt"}:
                raise failure

    if outcome == "success":
        use_connection()
    else:
        with pytest.raises(type(failure)) as caught:
            use_connection()
        assert caught.value is failure

    body_index = connection.events.index("body")
    expected = {
        "success": ["commit", "close"],
        "body_error": ["rollback", "close"],
        "commit_error": ["commit", "rollback", "close"],
        "interrupt": ["close"],
    }
    assert connection.events[body_index + 1 :] == expected[outcome]
    assert context.open_calls == 1


def test_open_failure_has_no_connection_to_close(context):
    context.open_failure = sqlite3.OperationalError("synthetic open failure")
    with pytest.raises(sqlite3.OperationalError) as caught, context.store.connect():
        pytest.fail("failed open must not enter the connection body")
    assert caught.value is context.open_failure
    assert context.open_calls == 1
    assert context.connection.events == []


def test_close_failure_retains_the_original_setup_error_as_context(context):
    connection = context.connection
    connection.failure_stage = "PRAGMA foreign_keys = ON"
    connection.close_failure = OSError("synthetic close failure")
    with pytest.raises(OSError) as caught, context.store.connect():
        pytest.fail("setup failure must not enter the connection body")
    assert caught.value is connection.close_failure
    assert caught.value.__context__ is connection.failure
    assert connection.events[-1] == "close"
    assert "commit" not in connection.events
    assert "rollback" not in connection.events
