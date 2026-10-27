"""Owner delivery over synthetic refs, isolated ledgers, and no live Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import threading

import pytest

from autostop_manager import role_coordination, telegram_bridge as bridge, telegram_wake as wake
from autostop_manager.role_coordination import RoleStore
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


ROOT = "/test/owner-snapshot"
REVISION = "a" * 40
OWNER = 42


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now]
    inbox = TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=lambda: clock[0])
    inbox.configure_instructions(ROOT, REVISION)
    inbox.duty_set(True, at=(now - timedelta(hours=1)).isoformat())
    inbox.configure_owner_route(OWNER)
    clock[0] += timedelta(seconds=1)
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    config = bridge.TelegramConfig(123456, "a" * 32, state_dir=tmp_path, socket_path=tmp_path / "bridge.sock")
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config, **_kwargs: OWNER)
    monkeypatch.setattr(wake, "validate_instruction_bundle", lambda _root, _revision: None)
    yield inbox, monitor, config, clock
    inbox.close()


def claim(inbox):
    return inbox.claim(instruction_root=ROOT, instruction_revision=REVISION, exclude_peer_id=OWNER)


def test_owner_delivery_does_not_acquire_client_slot_and_receipt_is_idempotent(runtime):
    inbox, _, _, _ = runtime
    owner = inbox.record(OWNER, 1)
    client = inbox.record(43, 2)
    job = claim(inbox)
    assert job["event_ids"] == [client]
    receipt = inbox.route_owner_event(
        event_id=owner,
        owner_peer_id=OWNER,
        receipt_id="owner-receipt-1",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    assert receipt["outcome"] == "routed"
    assert inbox.read_job(receipt["job_id"])["outcome"] == "routed"
    assert inbox.execution_slot()["job_id"] == job["job_id"]
    assert inbox.owner_pending(OWNER) == []
    assert inbox.owner_routes_unfinished() == [{"event_id": owner, "receipt_id": "owner-receipt-1"}]
    assert (
        inbox.route_owner_event(
            event_id=owner,
            owner_peer_id=OWNER,
            receipt_id="owner-receipt-1",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
        == receipt
    )
    with pytest.raises(InboxError, match="inbox_owner_receipt_conflict"):
        inbox.route_owner_event(
            event_id=owner,
            owner_peer_id=OWNER,
            receipt_id="owner-receipt-2",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    assert inbox.complete_owner_route(owner, "owner-receipt-1") == receipt
    assert inbox.owner_routes_unfinished() == []
    with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
        inbox.require_owner_source(client, OWNER)


def test_startup_cutover_holds_old_tests_and_survives_restart(tmp_path):
    path = tmp_path / "inbox" / "inbound.sqlite3"
    clock = [datetime(2026, 10, 27, 12, tzinfo=UTC)]
    with TelegramInbox(path, clock=lambda: clock[0]) as inbox:
        inbox.configure_instructions(ROOT, REVISION)
        inbox.duty_set(True, at=(clock[0] - timedelta(hours=1)).isoformat())
        old = inbox.record(OWNER, 1)
        clock[0] += timedelta(seconds=1)
        inbox.configure_owner_route(OWNER)
        generation = inbox.generation
        inbox.configure_owner_route(OWNER)
        assert inbox.generation == generation
        assert inbox.owner_pending(OWNER) == []
        with pytest.raises(InboxError, match="inbox_owner_event_not_pending"):
            inbox.require_owner_source(old, OWNER)
        old_job = inbox.active_job_page()["jobs"][0]
        assert old_job["reason_code"] == "owner_role_transition_review_required"
        assert inbox.execution_slot() is None
        assert inbox.job_status()["compatibility_hold"] is False
        clock[0] += timedelta(seconds=1)
        fresh = inbox.record(OWNER, 2)
        client = inbox.record(43, 3)
        assert claim(inbox)["event_ids"] == [client]
    with TelegramInbox(path, clock=lambda: clock[0]) as restored:
        restored.configure_instructions(ROOT, REVISION)
        generation = restored.generation
        restored.configure_owner_route(OWNER)
        assert restored.generation == generation
        assert [event["event_id"] for event in restored.owner_pending(OWNER)] == [fresh]
        # A historical update recovered only after restart keeps its original authority.
        recovered = restored.record(OWNER, 4, (clock[0] - timedelta(seconds=3)).isoformat())
        with pytest.raises(InboxError, match="inbox_owner_event_not_pending"):
            restored.require_owner_source(recovered, OWNER)
        receipt = restored.route_owner_event(
            event_id=fresh,
            owner_peer_id=OWNER,
            receipt_id="owner-restart",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    with TelegramInbox(path, clock=lambda: clock[0]) as restored:
        assert restored.owner_route_receipt(fresh) == receipt
        assert restored.require_owner_source(fresh, OWNER).message_id == 2


def test_owner_source_exact_read_after_routing_preserves_reply_reference(runtime, monkeypatch):
    inbox, monitor, config, _ = runtime
    event_id = inbox.record(OWNER, 25)
    client_id = inbox.record(43, 26)
    inbox.route_owner_event(
        event_id=event_id,
        owner_peer_id=OWNER,
        receipt_id="owner-receipt",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    source = SimpleNamespace(id=25, message="Разрешаю именно этот выпуск", reply_to_msg_id=24, media=None)
    reads = []

    async def load(_client, events):
        reads.extend((event.peer_id, event.message_id) for event in events)
        return [source]

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    response = asyncio.run(
        bridge._handle_operation(
            object(), config, {"operation": "owner_event_read", "event_id": event_id}, inbound_monitor=monitor
        )
    )
    assert response["role"] == "owner"
    assert response["source_message_id"] == 25 and response["reply_to_message_id"] == 24
    assert response["routing_receipt"]["receipt_id"] == "owner-receipt"
    assert reads == [(OWNER, 25)]
    with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
        asyncio.run(
            bridge._handle_operation(
                object(), config, {"operation": "owner_event_read", "event_id": client_id}, inbound_monitor=monitor
            )
        )
    assert len(reads) == 1


class OwnerJobs:
    def __init__(self, inbox, monitor, config):
        self.inbox, self.monitor, self.config = inbox, monitor, config
        self.connected = True
        self.owner_changed = asyncio.Event()
        self.calls = []
        self.fail_after_commit = False

    @property
    def snapshot(self):
        return {
            "owner_pending": len(self.inbox.owner_pending(OWNER)),
            "owner_routes_unfinished": len(self.inbox.owner_routes_unfinished()),
        }

    async def request(self, operation, **payload):
        self.calls.append(operation)
        result = await bridge._handle_job_control(
            object(), self.config, {"operation": operation, **payload}, self.monitor
        )
        if operation == "job_owner_routed" and self.fail_after_commit:
            self.fail_after_commit = False
            raise OSError("synthetic lost bridge receipt")
        return result


@pytest.mark.parametrize("loss", ["submit", "bridge"])
def test_lost_receipt_reuses_same_durable_event_and_never_starts_client(runtime, tmp_path, monkeypatch, loss):
    inbox, monitor, config, _ = runtime
    owner = inbox.record(OWNER, 1)
    store = RoleStore(tmp_path / "roles.sqlite3")
    submissions = []

    def submit(event_id, **payload):
        submissions.append(event_id)
        receipt = store.submit_owner_event(event_id, **payload)
        if loss == "submit":
            raise OSError("synthetic receipt lost after commit")
        return receipt

    monkeypatch.setattr(role_coordination, "submit_owner_event", submit)
    monkeypatch.setattr(role_coordination, "read_owner_receipt", store.owner_event_receipt)

    async def scenario():
        jobs = OwnerJobs(inbox, monitor, config)
        jobs.fail_after_commit = loss == "bridge"
        router = wake.OwnerEventRouter(
            jobs, wake.WakeConfig("synthetic", instruction_root=ROOT, instruction_revision=REVISION)
        )
        with pytest.raises(OSError):
            await router.deliver_pending()
        assert inbox.event_state(owner) == ("open" if loss == "submit" else "routed")
        router.config = wake.WakeConfig(
            "synthetic", instruction_root="/test/next-snapshot", instruction_revision="b" * 40
        )
        assert await router.deliver_pending() == (1 if loss == "submit" else 0)
        assert inbox.event_state(owner) == "routed"
        routed_job = inbox.read_job(inbox.owner_route_receipt(owner)["job_id"])
        assert (routed_job["instruction_root"], routed_job["instruction_revision"]) == (ROOT, REVISION)
        assert await router.deliver_pending() == 0
        assert submissions == [owner]
        assert inbox.execution_slot() is None
        assert store.role_state("M1")["desired_state"] == "off"
        assert store.role_state("M2")["desired_state"] == "off"

    asyncio.run(scenario())


def test_owner_route_failure_does_not_block_client_loop(runtime, monkeypatch):
    inbox, monitor, config, _ = runtime
    owner = inbox.record(OWNER, 1)
    client = inbox.record(43, 2)
    started, release = threading.Event(), threading.Event()

    def unavailable(_event_id, **_payload):
        started.set()
        release.wait(2)
        raise OSError("synthetic scheduler offline")

    monkeypatch.setattr(role_coordination, "submit_owner_event", unavailable)
    monkeypatch.setattr(role_coordination, "read_owner_receipt", lambda _event_id: None)

    async def scenario():
        jobs = OwnerJobs(inbox, monitor, config)
        router = wake.OwnerEventRouter(
            jobs, wake.WakeConfig("synthetic", instruction_root=ROOT, instruction_revision=REVISION)
        )
        jobs.owner_changed.set()
        task = asyncio.create_task(router.run())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert started.is_set()
            assert claim(inbox)["event_ids"] == [client]
            assert not release.is_set()
            release.set()
            for _ in range(100):
                if router.last_error:
                    break
                await asyncio.sleep(0.001)
            assert router.last_error == "wake_owner_delivery_pending"
            assert inbox.event_state(owner) == "open"
        finally:
            release.set()
            router.stopping = True
            jobs.owner_changed.set()
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["owner_event_read", "owner_event_voice_stage", "job_owner_pending"])
def test_owner_read_and_route_rpc_require_root_before_source_access(runtime, monkeypatch, operation):
    _, monitor, config, _ = runtime
    access = []

    def reject(_writer):
        access.append("root-check")
        raise bridge.BridgeError("root_required")

    monkeypatch.setattr(bridge, "_require_root_client", reject)

    class Reader:
        async def readline(self):
            return ('{"operation":"' + operation + '","event_id":"inbound-1"}\n').encode()

    class Writer:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    writer = Writer()
    asyncio.run(bridge._serve_client_request(object(), config, Reader(), writer, inbound_monitor=monitor))
    assert access == ["root-check"]
    assert b"root_required" in writer.body


def test_routed_owner_voice_uses_exact_source_and_cleanup_without_client_lease(runtime, monkeypatch):
    inbox, monitor, config, _ = runtime
    owner = inbox.record(OWNER, 1)
    client = inbox.record(43, 2)
    job = claim(inbox)
    inbox.route_owner_event(
        event_id=owner,
        owner_peer_id=OWNER,
        receipt_id="owner-voice",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    audio = b"OggSsynthetic-voice"
    attribute = type("DocumentAttributeAudio", (), {"voice": True, "duration": 8})()
    voice = SimpleNamespace(
        id=1,
        media=object(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[attribute]),
    )

    async def load(_client, events):
        assert [(event.peer_id, event.message_id) for event in events] == [(OWNER, 1)]
        return [voice]

    class Client:
        async def download_media(self, message, *, file):
            assert message is voice and file is bytes
            return audio

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: None)

    async def scenario():
        result = await bridge._handle_operation(
            Client(), config, {"operation": "owner_event_voice_stage", "event_id": owner}, inbound_monitor=monitor
        )
        handle = result["media_handle"]
        path = monitor.voice_stage(handle).path
        assert path.read_bytes() == audio
        discarded = await bridge._handle_operation(
            Client(), config, {"operation": "monitor_voice_discard", "media_handle": handle}, inbound_monitor=monitor
        )
        assert discarded["cleanup_verified"] is True and not path.exists()
        with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
            await bridge._handle_operation(
                Client(), config, {"operation": "owner_event_voice_stage", "event_id": client}, inbound_monitor=monitor
            )
        assert inbox.execution_slot()["job_id"] == job["job_id"]

    asyncio.run(scenario())
