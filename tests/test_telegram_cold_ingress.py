"""Artificial SDK metadata only; no Telegram connection, auth or real process read."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_ingress as ingress
from autostop_manager.cold_runtime import ColdBinding, ColdRuntimeDenied
from autostop_manager.telegram_inbox import TelegramInbox


@pytest.fixture
def current(monkeypatch):
    document = {
        "role": "personal-telegram",
        "attempt_id": "a",
        "boot_id": "b",
        "generation": "g",
        "epoch_id": "g",
        "source_revision": "c" * 40,
    }
    monkeypatch.setattr(ingress, "_process_identity", lambda: (101, 202))
    return ColdBinding(document)


class Client:
    async def get_me(self):
        return SimpleNamespace(id=12345, is_self=True)

    async def __call__(self, _request):
        return SimpleNamespace(pts=100, qts=0, seq=4)


def event(**overrides):
    return SimpleNamespace(
        **{
            "is_private": True,
            "out": False,
            "chat_id": 42,
            "id": 7,
            "date": datetime.now(UTC),
            "original_update": SimpleNamespace(pts=101),
            **overrides,
        }
    )


def capture(current, tmp_path):
    return asyncio.run(ingress.capture_cold_ingress(Client(), current, tmp_path, object()))


def test_cut_retains_its_counter_after_audit_file_edit(current, tmp_path):
    cut = capture(current, tmp_path)
    audit = tmp_path / "cold-ingress-cut.json"
    receipt = json.loads(audit.read_text())
    assert receipt["pts"] == 100 and receipt["main_pid"] == 101 and receipt["start_ticks"] == 202
    assert receipt["epoch_id"] == current.document["epoch_id"]
    assert receipt["observation_only"] is True and receipt["release_authority"] is False
    assert "12345" not in audit.read_text()
    assert audit.stat().st_mode & 0o777 == 0o600
    audit.write_text('{"pts":0}')
    assert not cut.accepts(event(original_update=SimpleNamespace(pts=100)))
    assert cut.accepts(event())
    with pytest.raises(FrozenInstanceError):
        cut.pts = 0


@pytest.mark.parametrize(
    "change",
    [
        {"original_update": None},
        {"original_update": SimpleNamespace(pts=True)},
        {"original_update": SimpleNamespace(pts=100)},
        {"original_update": SimpleNamespace(pts=99)},
        {"original_update": SimpleNamespace(pts=2**31)},
        {"date": None},
        {"date": datetime(2026, 10, 19, tzinfo=UTC).replace(tzinfo=None)},
        {"is_private": False},
        {"chat_id": True},
        {"id": True},
        {"out": True},
    ],
)
def test_old_or_unknown_update_never_gets_a_new_durable_operation(current, tmp_path, monkeypatch, change):
    cut = capture(current, tmp_path)
    monkeypatch.setattr(bridge, "binding", lambda: current)
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(enabled=True)
        monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
        monitor.cold_ingress_cut = cut
        assert monitor.record(event(**change)) is None
        assert inbox.events() == [] and inbox.active_jobs() == []


def test_new_update_gets_one_event_but_history_never_creates_cold_ids(current, tmp_path, monkeypatch):
    cut = capture(current, tmp_path)
    monkeypatch.setattr(bridge, "binding", lambda: current)
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(enabled=True)
        monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
        monitor.cold_ingress_cut = cut
        # A provider second straddling duty entry is held by the existing
        # boundary rule; it still cannot silently run an ambiguous operation.
        identifier = monitor.record(event())
        assert identifier is not None
        assert len(inbox.events()) == 1
        assert monitor.record(event()) == identifier
        asyncio.run(bridge._reconcile_incoming_history(object(), monitor))
        assert len(inbox.events()) == 1
        receipt = json.loads(cut.quarantine.path.read_text())
        assert receipt["status"] == "needs_review"
        assert receipt["historical_inventory_complete"] is False
        assert receipt["records"][0]["source"] == "history_not_queried"


def test_quarantine_retains_exact_private_locator_without_message_body(current, tmp_path, monkeypatch):
    cut = capture(current, tmp_path)
    monkeypatch.setattr(bridge, "binding", lambda: current)
    monitor = bridge.InboundMonitor()
    monitor.cold_ingress_cut = cut
    unknown = event(original_update=None, raw_text="synthetic-private-body-never-retain")
    before = monitor._next_sequence
    assert monitor.record(unknown) is None
    assert monitor._next_sequence == before and monitor.events() == []
    raw = cut.quarantine.path.read_text()
    receipt = json.loads(raw)
    assert receipt["records"][0]["locator"] == {"role": current.role, "peer_id": 42, "message_id": 7, "pts": None}
    assert receipt["status"] == "needs_review" and receipt["replay"] == "forbidden"
    assert "synthetic-private-body-never-retain" not in raw
    assert cut.quarantine.path.stat().st_mode & 0o777 == 0o600


def test_quarantine_overflow_is_bounded_and_never_claims_completeness(current, tmp_path):
    cut = capture(current, tmp_path)
    for identifier in range(258):
        cut.quarantine.hold(event(id=identifier + 1), source="callback", reason="cold_ingress_metadata_unknown")
    receipt = json.loads(cut.quarantine.path.read_text())
    assert len(receipt["records"]) == 256
    assert receipt["overflow_count"] == 2
    assert receipt["complete_observation_log"] is False
    assert receipt["status"] == "needs_review" and receipt["replay"] == "forbidden"
    assert receipt["overflow_chain_sha256"] != "0" * 64


def test_unowned_pending_observation_is_preserved_on_publication_failure(current, tmp_path):
    pending = tmp_path / ".cold-ingress-quarantine-101-202.json.pending"
    pending.write_bytes(b"synthetic-existing-observation")
    with pytest.raises(FileExistsError):
        capture(current, tmp_path)
    assert pending.read_bytes() == b"synthetic-existing-observation"
    assert not (tmp_path / "cold-ingress-cut.json").exists()


@pytest.mark.parametrize("metadata", ["account", "pts", "qts", "seq"])
def test_unknown_startup_metadata_publishes_no_cut(current, tmp_path, metadata):
    class InvalidClient(Client):
        async def get_me(self):
            return SimpleNamespace(id=12345, is_self=metadata != "account")

        async def __call__(self, _request):
            fields = {"pts": 100, "qts": 0, "seq": 4}
            fields[metadata] = True
            return SimpleNamespace(**fields)

    with pytest.raises(ColdRuntimeDenied):
        asyncio.run(ingress.capture_cold_ingress(InvalidClient(), current, tmp_path, object()))
    assert list(tmp_path.iterdir()) == []


def test_cold_daemon_observes_account_and_cut_before_any_handler(current, tmp_path, monkeypatch):
    order = []

    class DaemonClient(Client):
        def __init__(self, *_args, **_kwargs):
            self.session = SimpleNamespace(save_entities=True)

        async def connect(self):
            order.append("connect")

        async def is_user_authorized(self):
            order.append("authorized")
            return True

        async def get_me(self):
            order.append("account")
            return await super().get_me()

        async def __call__(self, request):
            order.append("cut")
            return await super().__call__(request)

        def add_event_handler(self, _callback, _builder):
            assert order == ["connect", "authorized", "account", "cut"]
            assert (tmp_path / "cold-ingress-cut.json").is_file()
            order.append("handler")

        async def disconnect(self):
            order.append("disconnect")

    async def stop_before_server(*_args, **_kwargs):
        raise OSError("synthetic server boundary")

    monkeypatch.setattr(bridge, "binding", lambda: current)
    monkeypatch.setattr(bridge, "_require_cold_telegram_config", lambda _config: None)
    monkeypatch.setattr(bridge, "_load_telethon", lambda: (DaemonClient, None, None))
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    monkeypatch.setattr(bridge, "_load_telegram_events", lambda: SimpleNamespace(NewMessage=lambda **_kw: object()))
    monkeypatch.setattr(bridge.asyncio, "start_unix_server", stop_before_server)
    config = bridge.TelegramConfig(
        1, "synthetic", session_path=tmp_path / "session", state_dir=tmp_path, socket_path=tmp_path / "bridge.sock"
    )
    with pytest.raises(OSError, match="synthetic server boundary"):
        asyncio.run(bridge.run_daemon(config, monitor_incoming=True))
    assert order == ["connect", "authorized", "account", "cut", "handler", "disconnect"]
