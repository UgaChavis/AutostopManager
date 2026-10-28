"""Fault proofs across real local IPC and durable storage, without live accounts."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
from telegram_wake_fakes import THREAD, MockServer, until
from test_telegram_bridge import _minimal_pdf
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_owner, telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox

OWNER = 20
CLIENT = 42
OTHER = 43
REVISION = "a" * 40


class User(SimpleNamespace):
    """Telethon-independent synthetic entity preserves production type checks."""


class TelegramClient:
    """Only synthetic messages; emulate a send committed before its response is lost."""

    def __init__(self):
        self.messages = {}
        self.sent = 0
        self.lose_send_response = False

    def inbound(self, identifier, *, peer=CLIENT, text="synthetic request"):
        result = SimpleNamespace(
            id=identifier,
            chat_id=peer,
            is_private=True,
            out=False,
            message=text,
            date=datetime.now(UTC),
            media=None,
            reply_to_msg_id=0,
        )
        self.messages[identifier] = result
        return result

    async def get_entity(self, identifier):
        return User(id=int(identifier), first_name="Synthetic", bot=False)

    async def get_messages(self, entity, *, ids=None, limit=None):
        if isinstance(ids, list):
            return [self.messages.get(identifier) for identifier in ids]
        if ids is not None:
            return self.messages.get(ids)
        peer = getattr(entity, "id", entity)
        rows = [message for message in self.messages.values() if entity is None or message.chat_id == peer]
        return sorted(rows, key=lambda message: message.id, reverse=True)[:limit]

    async def send_message(self, entity, text, **kwargs):
        self.sent += 1
        identifier = max(self.messages, default=0) + 1
        message = self.inbound(identifier, peer=getattr(entity, "id", entity), text=text)
        message.out = True
        message.reply_to_msg_id = kwargs.get("reply_to", 0)
        if self.lose_send_response:
            raise OSError("synthetic response loss")
        return message

    async def send_file(self, entity, upload, *, caption, **kwargs):
        content = upload.read()
        message = await self.send_message(entity, caption, reply_to=kwargs.get("reply_to", 0))
        message.media = SimpleNamespace()
        message.file = SimpleNamespace(
            name=upload.name,
            mime_type="application/pdf" if kwargs.get("force_document") else "image/jpeg",
            size=len(content),
        )
        message.document = SimpleNamespace(attributes=[])
        message.synthetic_content = content
        return message

    async def download_media(self, message, *, file):
        assert file is bytes
        return message.synthetic_content


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("real privileged bridge IPC requires root")
    releases = tmp_path / "releases"
    root = releases / "snapshot"
    for relative in (
        "AGENTS.md",
        "docs/agent/modules/B2.md",
        "docs/agent/modules/B4.md",
        ".agents/skills/manage-owner-telegram/SKILL.md",
        ".agents/skills/manage-autostop-client/SKILL.md",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic instruction bundle\n")
    (root / "REVISION").write_text(REVISION + "\n")
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (releases,))
    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.05,))
    utilities = SimpleNamespace(get_peer_id=lambda entity: entity.id)
    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, utilities, None))
    return root


@asynccontextmanager
async def bridge_runtime(tmp_path):
    state = tmp_path / "state"
    inbox = TelegramInbox(state / "inbound.sqlite3")
    inbox.duty_set(True, at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    owner_file = tmp_path / "owner.json"
    telegram_owner.configure_owner_peer_id(owner_file, OWNER, reader_gid=os.getgid())
    config = bridge.TelegramConfig(
        123456,
        "a" * 32,
        session_path=state / "account",
        state_dir=state,
        socket_path=tmp_path / "b.sock",
        owner_config_path=owner_file,
    )
    client = TelegramClient()
    writers = set()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            await bridge._serve_client(client, config, reader, writer, inbound_monitor=monitor)
        finally:
            writers.discard(writer)

    server = await asyncio.start_unix_server(serve, str(config.socket_path))
    os.chmod(config.socket_path, 0o600)
    jobs = wake.BridgeJobs(os.geteuid(), config.socket_path)
    runtime = SimpleNamespace(
        config=config,
        monitor=monitor,
        inbox=inbox,
        client=client,
        jobs=jobs,
        writers=writers,
    )
    try:
        yield runtime
    finally:
        await jobs.close()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
            await writer.wait_closed()
        await until(lambda: not writers)
        inbox.close()


@asynccontextmanager
async def worker(runtime, tmp_path, bundle, server):
    server.peer_threads = True
    path = str(tmp_path / "a.sock")
    async with unix_serve(server.handle, path, compression=None):
        app = wake.AppServer(
            wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
        )
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), runtime.jobs)
        dispatcher.start()
        try:
            yield dispatcher
        finally:
            await dispatcher.close()
            await app.close()


def binding(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


async def claim_running(runtime, bundle):
    job = (
        await runtime.jobs.request(
            "job_claim", instruction_root=str(bundle), instruction_revision=REVISION, worker_protocol=2
        )
    )["job"]
    assert job is not None
    prepared = (await runtime.jobs.request("job_peer_thread_prepare", **binding(job)))["peer_thread"]
    thread = prepared["thread_id"]
    if thread is None:
        thread = str(uuid5(NAMESPACE_OID, f"synthetic-client-{job['peer_id']}"))
        await runtime.jobs.request(
            "job_peer_thread_bind", **binding(job), creation_key=prepared["creation_key"], thread_id=thread
        )
    job = (await runtime.jobs.request("job_start_intent", **binding(job), thread_id=thread))["job"]
    return (await runtime.jobs.request("job_turn_bound", **binding(job), turn_id="synthetic-turn"))["job"]


async def send(runtime, job, *, kind, text, slot=0, dry=None):
    request = {
        "peer": str(job["peer_id"]),
        "text": text,
        "reply_to_message_id": runtime.inbox.resolve(job["event_ids"][0]).message_id,
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": kind,
        "effect_slot": slot,
        "mode": "apply" if dry else "dry_run",
    }
    if dry:
        request.update(contract_token=dry["contract_token"], idempotency_key=dry["idempotency_key"])
    return await runtime.jobs.request("send", **request)


async def model_complete(server):
    turn = str(server.counter)
    target = next(call["params"]["threadId"] for call in reversed(server.calls) if call.get("method") == "turn/start")
    server.turn_statuses[turn] = "completed"
    await server.sockets[-1].send(
        json.dumps(
            {"method": "turn/completed", "params": {"threadId": target, "turn": {"id": turn, "status": "completed"}}}
        )
    )


def test_three_clients_are_serial_and_reuse_only_their_own_context(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            peers = (CLIENT, OTHER, OTHER + 1)
            for identifier, peer in enumerate(peers, 100):
                runtime.monitor.record(runtime.client.inbound(identifier, peer=peer, text=f"synthetic-car-{peer}"))
            server = MockServer(complete=False, peer_threads=True)
            threads = {}
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                for sequence, peer in enumerate((*peers, CLIENT), 1):
                    if sequence == 4:
                        await until(lambda: runtime.inbox.execution_slot() is None)
                        runtime.monitor.record(runtime.client.inbound(200, peer=CLIENT, text="synthetic follow-up"))

                    def current(expected_peer=peer):
                        job = runtime.inbox.execution_slot()
                        return job if job and job["peer_id"] == expected_peer and job["state"] == "running" else None

                    await until(lambda: current() is not None)
                    job = current()
                    thread = job["thread_id"]
                    assert thread != THREAD
                    if peer in threads:
                        assert threads[peer] == thread
                    else:
                        assert thread not in threads.values()
                        threads[peer] = thread
                    assert server.counter == sequence
                    dry = await send(runtime, job, kind="result", text=f"synthetic reply {sequence}")
                    receipt = await send(runtime, job, kind="result", text=f"synthetic reply {sequence}", dry=dry)
                    await runtime.jobs.request(
                        "job_result",
                        **binding(job),
                        outcome="answered",
                        event_ids=job["event_ids"],
                        receipt_keys=[receipt["receipt_key"]],
                    )
                    # Delivery and business completion cannot free a still-running model turn.
                    await asyncio.sleep(0.01)
                    assert runtime.inbox.execution_slot()["job_id"] == job["job_id"]
                    assert server.counter == sequence
                    await model_complete(server)
                    await until(lambda target=sequence: dispatcher.completed == target)
                await until(lambda: runtime.inbox.execution_slot() is None)
                assert len(threads) == 3 and len(server.created_threads) == 3
                starts = [call for call in server.calls if call.get("method") == "turn/start"]
                assert [call["params"]["threadId"] for call in starts] == [threads[peer] for peer in (*peers, CLIENT)]
                assert not any(call.get("method") == "thread/fork" for call in server.calls)
                assert runtime.client.sent == 4 and runtime.inbox.status()["open_events"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(("kind", "outcome"), [("result", "answered"), ("clarification", "awaiting_client")])
def test_real_ipc_dispatcher_ack_result_and_exact_coverage(tmp_path, bundle, kind, outcome):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            refs = [runtime.monitor.record(runtime.client.inbound(identifier)) for identifier in (100, 101)]
            unrelated = runtime.monitor.record(runtime.client.inbound(110, peer=OTHER))
            server = MockServer(complete=False)
            server.release_start.clear()
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(lambda: server.started.is_set())
                job = runtime.inbox.active_jobs()[0]
                assert job["state"] == "start_intent" and job["turn_id"] is None
                assert job["event_ids"] == refs and job["thread_id"] != THREAD
                server.release_start.set()
                await until(lambda: runtime.inbox.read_job(job["job_id"])["state"] == "running")
                job = (await runtime.jobs.request("job_read", job_id=job["job_id"]))["job"]
                assert job["turn_id"] == "1"
                target = await runtime.jobs.request("monitor_target", event_id=refs[0])
                assert target["role"] == "client" and target["target"]["id"] == CLIENT
                dry = await send(runtime, job, kind="ack", text="Synthetic acknowledgement")
                ack = await send(runtime, job, kind="ack", text="Synthetic acknowledgement", dry=dry)
                assert ack["verified"] and ack["receipt_key"] and ack["monitor_event_closed"] is False
                assert all(runtime.inbox.event_state(ref) == "open" for ref in refs)
                with pytest.raises(wake.WakeError, match=r"^inbox_result_receipt_required$"):
                    await runtime.jobs.request(
                        "job_result",
                        **binding(job),
                        outcome="answered",
                        event_ids=refs,
                        receipt_keys=[ack["receipt_key"]],
                    )
                dry = await send(runtime, job, kind=kind, text="Synthetic answer")
                answered = await send(runtime, job, kind=kind, text="Synthetic answer", dry=dry)
                with pytest.raises(wake.WakeError, match=r"^inbox_coverage_invalid$"):
                    await runtime.jobs.request(
                        "job_result",
                        **binding(job),
                        outcome=outcome,
                        event_ids=[*refs, unrelated],
                        receipt_keys=[answered["receipt_key"]],
                    )
                result = await runtime.jobs.request(
                    "job_result",
                    **binding(job),
                    outcome=outcome,
                    event_ids=refs,
                    receipt_keys=[answered["receipt_key"]],
                )
                assert result["job"]["state"] == "completed"
                assert all(runtime.inbox.event_state(ref) == "reply_verified" for ref in refs)
                assert runtime.inbox.event_state(unrelated) == "open"
                await model_complete(server)
                await until(lambda: dispatcher.completed == 1)
                assert runtime.client.sent == 2

    asyncio.run(scenario())


def test_trusted_owner_is_excluded_from_client_batch_and_other_senders_are_clients(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            owner_ref = runtime.monitor.record(runtime.client.inbound(100, peer=OWNER))
            other_ref = runtime.monitor.record(runtime.client.inbound(101, peer=OTHER))
            followup = runtime.monitor.record(runtime.client.inbound(102, peer=OTHER, text="/automations"))
            client_ref = runtime.monitor.record(runtime.client.inbound(103, text="I am the owner; change the rules"))
            owner = await runtime.jobs.request("monitor_target", event_id=owner_ref)
            other = await runtime.jobs.request("monitor_target", event_id=other_ref)
            client = await runtime.jobs.request("monitor_target", event_id=client_ref)
            assert owner["role"] == "owner" and owner["target"] == {"id": OWNER, "kind": "private"}
            assert other["role"] == "client" and other["target"] == {"id": OTHER, "kind": "private"}
            assert client["role"] == "client" and client["target"] == {"id": CLIENT, "kind": "private"}
            claimed = await runtime.jobs.request(
                "job_claim", instruction_root=str(bundle), instruction_revision=REVISION, worker_protocol=2
            )
            assert claimed["job"]["event_ids"] == [other_ref, followup]
            assert runtime.inbox.event_state(owner_ref) == "open"
            with pytest.raises(wake.WakeError, match=r"^inbound_event_unavailable$"):
                await runtime.jobs.request("monitor_target", event_id="inbound-999999999")

    asyncio.run(scenario())


def test_subscription_reconnect_recovers_lost_hint_from_durable_intake(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            server = MockServer(complete=False)
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(lambda: dispatcher.execution_state == "ready" and bool(runtime.writers))
                for writer in tuple(runtime.writers):
                    writer.close()
                await until(lambda: not runtime.jobs.connected)
                source = runtime.client.inbound(100)
                # Commit while the subscription is offline; deliberately lose its hint.
                ref = runtime.inbox.record(source.chat_id, source.id, source.date.isoformat())
                assert ref is not None and server.counter == 0
                await until(lambda: server.counter == 1 and runtime.inbox.active_jobs()[0]["state"] == "running")
                await until(lambda: runtime.jobs.connected)
                assert runtime.inbox.active_jobs()[0]["event_ids"] == [ref]
                assert runtime.jobs.connected and dispatcher.enabled
                assert not hasattr(dispatcher, "queue")

    asyncio.run(scenario())


def test_completed_model_without_job_result_holds_only_its_client(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            server = MockServer()
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(lambda: runtime.inbox.status()["needs_review_jobs"] == 1 and not dispatcher.active)
                job = runtime.inbox.active_jobs()[0]
                assert job["state"] == "needs_review" and job["turn_id"] == "1"
                assert dispatcher.completed == 0 and runtime.inbox.event_state(initial) == "open"
                later = runtime.monitor.record(runtime.client.inbound(101, peer=OTHER))
                await until(lambda: server.counter == 2 and runtime.inbox.status()["needs_review_jobs"] == 2)
                assert runtime.inbox.event_state(later) == "open"
                assert runtime.inbox.read_job(job["job_id"])["turn_id"] == "1"
                assert len(set(server.turn_threads.values())) == 2
                status = await runtime.jobs.request("job_status")
                assert status["status"]["open_events"] == 2 and status["status"]["enabled"]
                assert status["status"]["needs_review_jobs"] == 2
                await until(lambda: runtime.inbox.execution_slot() is None and dispatcher.status()["ready"])
                assert dispatcher.status()["ready"]

    asyncio.run(scenario())


def test_warm_thread_failure_holds_acknowledged_job_without_replay(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            server = MockServer(complete=False)
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(
                    lambda: runtime.inbox.active_jobs() and runtime.inbox.active_jobs()[0]["state"] == "running"
                )
                job = runtime.inbox.active_jobs()[0]
                dry = await send(runtime, job, kind="ack", text="Synthetic acknowledgement")
                receipt = await send(runtime, job, kind="ack", text="Synthetic acknowledgement", dry=dry)
                server.turn_statuses[job["turn_id"]] = "failed"
                failure = json.dumps(
                    {
                        "method": "thread/status/changed",
                        "params": {"threadId": job["thread_id"], "status": {"type": "systemError"}},
                    }
                )
                await server.sockets[-1].send(failure)
                await until(lambda: not dispatcher.active and runtime.inbox.execution_slot() is None)
                held = runtime.inbox.read_job(job["job_id"])
                assert held["state"] == "needs_review" and held["turn_id"] == job["turn_id"]
                assert held["reason_code"] == "codex_thread_system_error"
                assert held["effects"][0]["receipt_key"] == receipt["receipt_key"]
                assert dispatcher.completed == 0
                newer = runtime.monitor.record(runtime.client.inbound(110, peer=OTHER))
                await until(lambda: server.counter == 2)
                # A late notification for A cannot interrupt B's own thread.
                await server.sockets[-1].send(failure)
                await until(lambda: runtime.jobs.generation == runtime.inbox.generation)
                await asyncio.sleep(0.02)
                assert server.counter == 2 and runtime.client.sent == 1
                assert runtime.inbox.event_state(initial) == runtime.inbox.event_state(newer) == "open"
                assert runtime.inbox.status()["enabled"] and runtime.inbox.status()["needs_review_jobs"] == 1
                assert dispatcher.app.config.thread_id != job["thread_id"]
            # A new executor connection may be healthy; the saved job still
            # requires review and cannot replay either its model or its ack.
            async with worker(runtime, tmp_path, bundle, server) as replacement:
                await until(lambda: replacement.status()["ready"] and not replacement.active)
                assert server.counter == 2 and runtime.client.sent == 1
                assert runtime.inbox.read_job(job["job_id"])["turn_id"] == job["turn_id"]
                assert runtime.inbox.status()["open_events"] == 2

    asyncio.run(scenario())


def test_new_telegram_update_before_handler_rejects_stale_dryrun(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            update = runtime.client.inbound(101, text="Synthetic quantity update")
            with pytest.raises(wake.WakeError, match=r"^inbox_context_stale$"):
                await send(runtime, job, kind="result", text="Stale synthetic answer")
            assert runtime.client.sent == 0
            current_ref = runtime.inbox.events()[-1]["event_id"]
            assert runtime.inbox.resolve(current_ref).message_id == update.id
            refreshed = (await runtime.jobs.request("job_refresh", **binding(job), event_ids=[initial, current_ref]))[
                "job"
            ]
            dry = await send(runtime, refreshed, kind="result", text="Updated synthetic answer")
            answered = await send(runtime, refreshed, kind="result", text="Updated synthetic answer", dry=dry)
            await runtime.jobs.request(
                "job_result",
                **binding(refreshed),
                outcome="answered",
                event_ids=refreshed["event_ids"],
                receipt_keys=[answered["receipt_key"]],
            )
            assert runtime.inbox.status()["open_events"] == 0 and runtime.client.sent == 1

    asyncio.run(scenario())


def test_lost_send_response_blocks_replay_new_slot_and_revision(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            dry = await send(runtime, job, kind="result", text="Synthetic answer")
            runtime.client.lose_send_response = True
            with pytest.raises(wake.WakeError, match=r"^telegram_operation_failed$"):
                await send(runtime, job, kind="result", text="Synthetic answer", dry=dry)
            assert runtime.client.sent == 1
            effect = runtime.inbox.read_effect(dry["idempotency_key"])
            assert effect["attempted"] == 1 and effect["verified"] == 0
            with pytest.raises(wake.WakeError, match=r"^send_outcome_uncertain$"):
                await send(runtime, job, kind="result", text="Synthetic answer", dry=dry)
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await send(runtime, job, kind="result", text="Alternative synthetic answer", slot=1)
            newer = runtime.monitor.record(runtime.client.inbound(110))
            refreshed = (await runtime.jobs.request("job_refresh", **binding(job), event_ids=[initial, newer]))["job"]
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await send(runtime, refreshed, kind="result", text="Revised synthetic answer")
            assert runtime.client.sent == 1 and runtime.inbox.status()["open_events"] == 2
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await runtime.jobs.request(
                    "job_result",
                    **binding(refreshed),
                    outcome="no_reply_needed",
                    event_ids=refreshed["event_ids"],
                    receipt_keys=[],
                    reason_code="not_actionable",
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(("operation", "suffix"), [("send_photo", ".jpg"), ("send_document", ".pdf")])
def test_job_bound_media_receipt_is_verified_and_replay_does_not_resend(tmp_path, bundle, operation, suffix):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            ref = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            outbox = runtime.config.socket_path.parent / "outbox"
            outbox.mkdir(mode=0o700)
            path = outbox / ("synthetic" + suffix)
            path.write_bytes(b"\xff\xd8synthetic\xff\xd9" if suffix == ".jpg" else _minimal_pdf())
            path.chmod(0o600)
            request = {
                "peer": str(CLIENT),
                "photo" if suffix == ".jpg" else "document": str(path),
                "caption": "Synthetic result attachment",
                "mode": "dry_run",
                "reply_to_message_id": 100,
                "job_id": job["job_id"],
                "lease": job["lease"],
                "revision": job["revision"],
                "kind": "result",
            }
            dry = await runtime.jobs.request(operation, **request)
            request.update(mode="apply", contract_token=dry["contract_token"], idempotency_key=dry["idempotency_key"])
            sent = await runtime.jobs.request(operation, **request)
            repeated = await runtime.jobs.request(operation, **request)
            assert sent["verified"] and repeated["replayed"] and repeated["receipt_key"] == sent["receipt_key"]
            assert runtime.client.sent == 1 and runtime.inbox.event_state(ref) == "open"
            await runtime.jobs.request(
                "job_result",
                **binding(job),
                outcome="answered",
                event_ids=[ref],
                receipt_keys=[sent["receipt_key"]],
            )
            assert runtime.inbox.event_state(ref) == "reply_verified"

    asyncio.run(scenario())


def test_handoff_completes_only_after_verified_exact_owner_notification(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            ref = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            request = binding(job) | {
                "reply_to_message_id": 100,
                "text": "Synthetic request requires a human",
                "mode": "dry_run",
            }
            dry = await runtime.jobs.request("job_handoff", **request)
            assert runtime.client.sent == 0 and runtime.inbox.event_state(ref) == "open"
            with pytest.raises(wake.WakeError, match=r"^inbox_result_receipt_required$"):
                await runtime.jobs.request(
                    "job_result", **binding(job), outcome="handoff", event_ids=[ref], receipt_keys=[]
                )
            receipt = await runtime.jobs.request(
                "job_handoff",
                **(
                    request
                    | {
                        "mode": "apply",
                        "contract_token": dry["contract_token"],
                        "idempotency_key": dry["idempotency_key"],
                    }
                ),
            )
            assert receipt["verified"] and runtime.client.sent == 1
            outgoing = [message for message in runtime.client.messages.values() if message.out]
            assert len(outgoing) == 1 and outgoing[0].chat_id == OWNER
            assert runtime.inbox.event_state(ref) == "open"
            result = await runtime.jobs.request(
                "job_result", **binding(job), outcome="handoff", event_ids=[ref], receipt_keys=[receipt["receipt_key"]]
            )
            assert result["job"]["outcome"] == "handoff" and runtime.inbox.event_state(ref) == "reply_verified"

    asyncio.run(scenario())


def test_nonroot_ipc_control_rejection_is_structured_and_connection_closes(tmp_path, bundle, monkeypatch):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            original_unpack = struct.unpack

            def unprivileged(format_string, data):
                values = original_unpack(format_string, data)
                return (values[0], 65534, values[2]) if format_string == "3i" else values

            monkeypatch.setattr(bridge.struct, "unpack", unprivileged)
            reader, writer = await asyncio.open_unix_connection(str(runtime.config.socket_path))
            try:
                writer.write(b'{"operation":"job_claim"}\n')
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 1))
                assert response == {"ok": False, "error": "job_control_permission_denied"}
                assert await asyncio.wait_for(reader.read(), 1) == b""
                assert runtime.inbox.active_jobs() == []
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


def test_two_prepared_sends_cannot_bypass_a_lost_remote_receipt(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            ref = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            first = await send(runtime, job, kind="result", text="Synthetic first answer", slot=0)
            second = await send(runtime, job, kind="result", text="Synthetic second answer", slot=1)
            runtime.client.lose_send_response = True
            with pytest.raises(wake.WakeError):
                await send(runtime, job, kind="result", text="Synthetic first answer", slot=0, dry=first)
            runtime.client.lose_send_response = False
            assert runtime.client.sent == 1
            assert len([message for message in runtime.client.messages.values() if message.out]) == 1
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await send(runtime, job, kind="result", text="Synthetic second answer", slot=1)
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await send(runtime, job, kind="result", text="Synthetic second answer", slot=1, dry=second)
            effect = runtime.inbox.read_effect(first["idempotency_key"])
            assert effect["attempted"] == 1 and effect["verified"] == 0
            assert runtime.client.sent == 1 and runtime.inbox.event_state(ref) == "open"

    asyncio.run(scenario())


def test_customer_receipt_cannot_impersonate_a_human_handoff(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            ref = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            with pytest.raises(wake.WakeError, match=r"^job_handoff_required$"):
                await send(runtime, job, kind="handoff", text="Synthetic acknowledgement to the customer")
            with pytest.raises(wake.WakeError, match=r"^inbox_result_receipt_required$"):
                await runtime.jobs.request(
                    "job_result", **binding(job), outcome="handoff", event_ids=[ref], receipt_keys=[]
                )
            assert runtime.client.sent == 0 and runtime.inbox.event_state(ref) == "open"

    asyncio.run(scenario())


def test_held_job_pages_fit_real_bridge_and_legacy_cli_limits(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            count = 300
            runtime.inbox.import_references(
                [
                    {
                        "event_id": f"inbound-{index}",
                        "peer_id": 1000 + index,
                        "message_id": index,
                        "received_at": "2026-10-12T00:00:00+00:00",
                        "state": "open",
                    }
                    for index in range(1, count + 1)
                ],
                instruction_root=str(bundle),
                instruction_revision=REVISION,
            )
            initial = await asyncio.to_thread(
                bridge.send_local_request, runtime.config.socket_path, {"operation": "job_active"}
            )
            assert initial["ok"] is True and len(initial["jobs"]) == 50
            seen = set()
            cursor = None
            while True:
                page = await runtime.jobs.request("job_active", limit=50, after_job_id=cursor)
                assert len(json.dumps(page).encode()) < 128 * 1024
                assert len(page["jobs"]) <= 50
                for job in page["jobs"]:
                    assert job["state"] == "needs_review" and "effects" not in job
                    assert job["job_id"] not in seen
                    seen.add(job["job_id"])
                cursor = page["next_job_id"]
                if cursor is None:
                    break
            assert len(seen) == count and runtime.inbox.status()["open_events"] == count
            assert runtime.client.sent == 0

    asyncio.run(scenario())


def test_explicit_pause_survives_a_worker_restart_with_pending_intake(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            server = MockServer(complete=False)
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(lambda: dispatcher.status()["ready"])
                await dispatcher.pause()
                ref = runtime.monitor.record(runtime.client.inbound(100))
                assert runtime.inbox.status()["execution_paused"] is True
                assert runtime.inbox.event_state(ref) == "open" and server.counter == 0
            runtime.jobs = wake.BridgeJobs(os.geteuid(), runtime.config.socket_path)
            async with worker(runtime, tmp_path, bundle, server) as restarted:
                await until(lambda: restarted.execution_state == "paused" and restarted.app.connected)
                assert restarted.status()["ready"] is False
                assert runtime.inbox.event_state(ref) == "open" and server.counter == 0
                await restarted.resume_execution()
                await until(lambda: server.started.is_set())
                assert runtime.inbox.status()["execution_paused"] is False
                assert server.counter == 1
                assert runtime.inbox.active_jobs()[0]["event_ids"] == [ref]

    asyncio.run(scenario())


@pytest.mark.parametrize("refresh_first", [False, True])
def test_old_send_readback_after_new_inbound_does_not_replay_or_close_new_context(tmp_path, bundle, refresh_first):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            dry = await send(runtime, job, kind="result", text="Synthetic first answer")
            get_messages = runtime.client.get_messages
            lost = False

            async def lose_first_readback(entity, *, ids=None, limit=None):
                nonlocal lost
                if ids == 101 and not lost:
                    lost = True
                    return None
                return await get_messages(entity, ids=ids, limit=limit)

            runtime.client.get_messages = lose_first_readback
            with pytest.raises(wake.WakeError, match=r"^send_readback_failed$"):
                await send(runtime, job, kind="result", text="Synthetic first answer", dry=dry)
            assert runtime.client.sent == 1
            newer = runtime.monitor.record(runtime.client.inbound(102, text="Synthetic clarification"))
            if refresh_first:
                job = (await runtime.jobs.request("job_refresh", **binding(job), event_ids=[initial, newer]))["job"]
            proof = await runtime.jobs.request("job_effect_readback", **binding(job), effect_key=dry["idempotency_key"])
            assert proof["verified"] is True and proof["receipt_key"] == dry["idempotency_key"]
            assert proof["message_id"] == 101 and runtime.client.sent == 1
            assert runtime.inbox.read_effect(dry["idempotency_key"])["verified"] == 1
            assert runtime.inbox.event_state(initial) == runtime.inbox.event_state(newer) == "open"
            if not refresh_first:
                job = (await runtime.jobs.request("job_refresh", **binding(job), event_ids=[initial, newer]))["job"]
            with pytest.raises(wake.WakeError, match=r"^inbox_result_receipt_required$"):
                await runtime.jobs.request(
                    "job_result",
                    **binding(job),
                    outcome="answered",
                    event_ids=job["event_ids"],
                    receipt_keys=[proof["receipt_key"]],
                )
            prepared = await send(runtime, job, kind="result", text="Synthetic revised answer")
            revised = await send(runtime, job, kind="result", text="Synthetic revised answer", dry=prepared)
            finished = await runtime.jobs.request(
                "job_result",
                **binding(job),
                outcome="answered",
                event_ids=job["event_ids"],
                receipt_keys=[proof["receipt_key"], revised["receipt_key"]],
            )
            assert finished["job"]["state"] == "completed" and runtime.client.sent == 2
            assert runtime.inbox.event_state(initial) == runtime.inbox.event_state(newer) == "reply_verified"

    asyncio.run(scenario())


def test_paused_operator_finishes_terminal_job_with_verified_effect_and_new_context(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            initial = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            dry = await send(runtime, job, kind="result", text="Synthetic original answer")
            get_messages = runtime.client.get_messages
            lost = False

            async def lose_first_readback(entity, *, ids=None, limit=None):
                nonlocal lost
                if ids == 101 and not lost:
                    lost = True
                    return None
                return await get_messages(entity, ids=ids, limit=limit)

            runtime.client.get_messages = lose_first_readback
            with pytest.raises(wake.WakeError, match=r"^send_readback_failed$"):
                await send(runtime, job, kind="result", text="Synthetic original answer", dry=dry)
            newer = runtime.monitor.record(runtime.client.inbound(102, text="Synthetic clarification"))
            job = (
                await runtime.jobs.request(
                    "job_needs_review", **binding(job), reason_code="codex_completed_without_job_result"
                )
            )["job"]
            await runtime.jobs.request("job_execution_set", paused=True, expected_revision=1)
            server = MockServer(complete=False)
            server.turn_statuses[job["turn_id"]] = "completed"
            async with worker(runtime, tmp_path, bundle, server) as dispatcher:
                await until(lambda: dispatcher.execution_state == "paused" and dispatcher.app.connected)
                proof = await runtime.jobs.request(
                    "job_effect_readback", **binding(job), effect_key=dry["idempotency_key"]
                )
                assert proof["verified"] and runtime.client.sent == 1
                with pytest.raises(wake.WakeError, match=r"^inbox_execution_proof_required$"):
                    await runtime.jobs.request(
                        "job_resolve", **binding(job), decision="continue_bound_turn", effects_reviewed=True
                    )
                metadata = await dispatcher.app.request(
                    "thread/turns/list", {"threadId": job["thread_id"], "limit": 100}
                )
                assert {"id": job["turn_id"], "status": "completed"} in metadata["data"]
                job = (
                    await runtime.jobs.request(
                        "job_execution_proof",
                        **binding(job),
                        proof_kind="terminal",
                        thread_id=job["thread_id"],
                        turn_id=job["turn_id"],
                        terminal_status="completed",
                        local_tasks_done=True,
                    )
                )["job"]
                with pytest.raises(wake.WakeError, match=r"^inbox_retry_effects_present$"):
                    await runtime.jobs.request(
                        "job_resolve", **binding(job), decision="retry_no_effects", effects_reviewed=True
                    )
                job = (
                    await runtime.jobs.request(
                        "job_resolve", **binding(job), decision="continue_bound_turn", effects_reviewed=True
                    )
                )["job"]
                job = (await runtime.jobs.request("job_refresh", **binding(job), event_ids=[initial, newer]))["job"]
                with pytest.raises(wake.WakeError, match=r"^inbox_result_receipt_required$"):
                    await runtime.jobs.request(
                        "job_result",
                        **binding(job),
                        outcome="answered",
                        event_ids=job["event_ids"],
                        receipt_keys=[proof["receipt_key"]],
                    )
                revised = await send(runtime, job, kind="result", text="Synthetic revised answer")
                revised = await send(runtime, job, kind="result", text="Synthetic revised answer", dry=revised)
                finished = await runtime.jobs.request(
                    "job_result",
                    **binding(job),
                    outcome="answered",
                    event_ids=job["event_ids"],
                    receipt_keys=[proof["receipt_key"], revised["receipt_key"]],
                )
                assert finished["job"]["state"] == "completed"
                assert runtime.inbox.status()["execution_paused"] is True
                assert runtime.inbox.event_state(initial) == runtime.inbox.event_state(newer) == "reply_verified"
                assert runtime.client.sent == 2 and server.counter == 0
                await dispatcher.resume_execution()
                await until(lambda: dispatcher.status()["ready"])
                assert runtime.client.sent == 2 and server.counter == 0

    asyncio.run(scenario())


def test_pause_if_idle_via_two_ipc_sockets_and_client_process(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(901))
            app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), runtime.jobs)
            socket = tmp_path / "wake.sock"
            async with await asyncio.start_unix_server(dispatcher.serve, path=str(socket)):
                script = (
                    "import asyncio,json,sys; from pathlib import Path; "
                    "from autostop_manager.telegram_wake import local_request; "
                    'print(json.dumps(asyncio.run(local_request({"operation":"pause-if-idle"}, Path(sys.argv[1])))))'
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    script,
                    str(socket),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                assert process.returncode == 0, stderr.decode()
                assert json.loads(stdout)["execution_paused"] is True
                assert runtime.inbox.status()["execution_paused"] is True
                assert runtime.inbox.resolve(event).message_id == 901
                assert runtime.inbox.status()["global_slot"] is None
                result = await runtime.jobs.request(
                    "job_claim",
                    instruction_root=str(bundle),
                    instruction_revision=REVISION,
                    worker_protocol=wake.WORKER_PROTOCOL,
                )
                assert result["job"] is None
                assert runtime.client.sent == 0

    asyncio.run(scenario())


def test_pause_if_idle_busy_over_ipc_preserves_running_job(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(902))
            job = await claim_running(runtime, bundle)
            before = runtime.inbox.read_job(job["job_id"])
            app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), runtime.jobs)
            socket = tmp_path / "wake.sock"
            async with await asyncio.start_unix_server(dispatcher.serve, path=str(socket)):
                result = await wake.local_request({"operation": "pause-if-idle"}, socket)
                assert result == {"ok": False, "error": "inbox_execution_slot_occupied"}
                assert runtime.inbox.read_job(job["job_id"]) == before
                assert runtime.inbox.status()["execution_paused"] is False
                assert runtime.inbox.resolve(event).message_id == 902
                assert runtime.client.sent == 0

    asyncio.run(scenario())


def test_pause_if_idle_old_bridge_rejects_without_fallback(tmp_path, bundle, monkeypatch):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(903))
            original = runtime.jobs.request
            calls = []

            async def old_bridge(operation, **arguments):
                calls.append(operation)
                if operation == "job_execution_pause_if_idle":
                    raise wake.WakeError("operation_not_supported")
                return await original(operation, **arguments)

            monkeypatch.setattr(runtime.jobs, "request", old_bridge)
            app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), runtime.jobs)
            with pytest.raises(wake.WakeError, match="operation_not_supported"):
                await dispatcher.pause_if_idle()
            assert calls == ["job_status", "job_execution_pause_if_idle"]
            assert runtime.inbox.status()["execution_paused"] is False
            assert runtime.inbox.resolve(event).message_id == 903
            assert not dispatcher.pause_requested and dispatcher.enabled
            assert runtime.client.sent == 0

    asyncio.run(scenario())


def test_pause_if_idle_lost_reply_is_not_retried(tmp_path, bundle, monkeypatch):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(904))
            original = runtime.jobs.request
            calls = []

            async def lost_reply(operation, **arguments):
                calls.append(operation)
                result = await original(operation, **arguments)
                if operation == "job_execution_pause_if_idle":
                    raise wake.TransportLost("wake_bridge_disconnected")
                return result

            monkeypatch.setattr(runtime.jobs, "request", lost_reply)
            app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), runtime.jobs)
            with pytest.raises(wake.TransportLost):
                await dispatcher.pause_if_idle()
            assert calls == ["job_status", "job_execution_pause_if_idle"]
            assert runtime.inbox.status()["execution_paused"] is True
            assert runtime.inbox.resolve(event).message_id == 904
            assert not dispatcher.pause_requested and dispatcher.enabled
            assert runtime.client.sent == 0

    asyncio.run(scenario())


def test_pause_claim_concurrent_ipc_preserves_event_and_slot(tmp_path, bundle):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(905))
            revision = runtime.inbox.status()["execution_revision"]
            claim, pause = await asyncio.gather(
                runtime.jobs.request(
                    "job_claim",
                    instruction_root=str(bundle),
                    instruction_revision=REVISION,
                    worker_protocol=wake.WORKER_PROTOCOL,
                ),
                runtime.jobs.request("job_execution_pause_if_idle", expected_revision=revision),
                return_exceptions=True,
            )
            assert not isinstance(claim, Exception)
            job = claim["job"]
            if job is None:
                assert not isinstance(pause, Exception)
                assert pause["status"]["execution_paused"] is True
                assert runtime.inbox.status()["global_slot"] is None
            else:
                assert isinstance(pause, wake.WakeError)
                assert str(pause) == "inbox_execution_slot_occupied"
                assert runtime.inbox.status()["execution_paused"] is False
                assert runtime.inbox.status()["global_slot"]["job_id"] == job["job_id"]
                assert runtime.inbox.read_job(job["job_id"])["event_ids"] == [event]
            assert runtime.inbox.resolve(event).message_id == 905
            assert runtime.client.sent == 0

    asyncio.run(scenario())
