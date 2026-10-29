"""Review regressions through real inbox/bridge handlers and private transport."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from unittest.mock import AsyncMock

import pytest
from test_telegram_bridge_durable import Client, _job, _message, _runtime, _send
from test_telegram_bridge_durable import synthetic_state_request as synthetic_state_request
from test_telegram_human_control import close_turn, guard, manual
from test_telegram_wake_durable import FakeJobs

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_effects import rpc_budget, server_budget
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


@pytest.mark.parametrize("action,reason", [("pause", "paused"), ("takeover", "human_takeover")])
@pytest.mark.parametrize("resume_before_proof", [False, True])
def test_client_resume_preserves_original_question_and_verified_ack(tmp_path, action, reason, resume_before_proof):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            incoming = _message(100, text="Synthetic maintenance question")
            event = monitor.record(incoming)
            job = _job(monitor)
            client = Client([incoming])
            dry = await _send(client, config, monitor, job, kind="ack", text="Synthetic acknowledgement")
            ack = await _send(
                client, config, monitor, job, kind="ack", text="Synthetic acknowledgement", mode="apply", dry=dry
            )
            control = inbox.set_client_control(42, action=action, expected_revision=0)
            parked = inbox.result(
                **guard(job),
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[ack["receipt_key"]],
                reason_code=reason,
            )
            assert parked["state"] == "waiting_control" and parked["outcome"] is None
            assert inbox.event_state(event) == "open"
            if resume_before_proof:
                inbox.set_client_control(42, action="resume", expected_revision=control["revision"])
                assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            stopped = close_turn(inbox, parked)
            if not resume_before_proof:
                assert stopped["state"] == "waiting_control"
                inbox.set_client_control(42, action="resume", expected_revision=control["revision"])
            resumed = _job(monitor)
            assert resumed["event_ids"] == [event]
            assert [row["receipt_key"] for row in resumed["prior_acknowledgements"]] == [ack["receipt_key"]]
            assert resumed["effects"] == []
            dry = await _send(client, config, monitor, resumed, kind="result", text="Synthetic maintenance result")
            sent = await _send(
                client,
                config,
                monitor,
                resumed,
                kind="result",
                text="Synthetic maintenance result",
                mode="apply",
                dry=dry,
            )
            done = inbox.result(
                **guard(resumed), outcome="answered", event_ids=[event], receipt_keys=[sent["receipt_key"]]
            )
            close_turn(inbox, done)
            assert client.sent == 2 and inbox.event_state(event) == "reply_verified"
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_paused_request_survives_reopen_and_unknown_send_cannot_be_parked(tmp_path):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    event = monitor.record(_message(100))
    job = _job(monitor)
    control = inbox.set_client_control(42, action="pause", expected_revision=0)
    parked = inbox.result(
        **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="paused"
    )
    close_turn(inbox, parked)
    path = inbox.path
    inbox.close()
    with TelegramInbox(path) as restored:
        assert restored.event_state(event) == "open"
        restored.set_client_control(42, action="resume", expected_revision=control["revision"])
        monitor = bridge.DurableInboundMonitor(restored, enabled=True)
        resumed = _job(monitor)
        effect = restored.reserve_effect(
            **guard(resumed), kind="result", text_sha256="a" * 64, peer_id=42, reply_to_message_id=100
        )
        restored.mark_effect_intent(effect)
        restored.set_client_control(42, action="pause", expected_revision=2)
        with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
            restored.result(
                **guard(resumed), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="paused"
            )
        assert restored.read_job(resumed["job_id"])["state"] == "running"


def test_followup_arriving_before_defer_is_answered_with_original_once(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            incoming, followup = _message(100), _message(101, text="Synthetic progress question")
            first = monitor.record(incoming)
            job = _job(monitor, running=False)
            mapping = inbox.prepare_peer_thread(**guard(job))
            inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="test-thread")
            job = inbox.start_intent(**guard(job), thread_id="test-thread")
            second = monitor.record(followup)
            stopped = inbox.finish_execution(
                **guard(job),
                proof_kind="start_rejected",
                thread_id=job["thread_id"],
                local_tasks_done=True,
            )
            inbox.defer_failed_request(**guard(stopped))
            recovered = _job(monitor)
            assert set(recovered["event_ids"]) == {first, second}
            client = Client([incoming, followup])
            dry = await _send(client, config, monitor, recovered, kind="result", text="Synthetic useful result")
            sent = await _send(
                client, config, monitor, recovered, kind="result", text="Synthetic useful result", mode="apply", dry=dry
            )
            done = inbox.result(
                **guard(recovered),
                outcome="answered",
                event_ids=recovered["event_ids"],
                receipt_keys=[sent["receipt_key"]],
            )
            close_turn(inbox, done)
            latest = monitor.record(_message(103, text="Synthetic independent question"))
            assert _job(monitor)["event_ids"] == [latest]
            assert inbox.event_state(first) == inbox.event_state(second) == "reply_verified" and client.sent == 1
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("resume_before_recovery", [False, True])
def test_parked_request_reopen_before_proof_keeps_exact_wake_recovery(tmp_path, monkeypatch, resume_before_recovery):
    async def scenario():
        _config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        event = monitor.record(_message(100))
        job = _job(monitor)
        inbox.set_client_control(42, action="pause", expected_revision=0)
        parked = inbox.result(
            **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="paused"
        )
        path = inbox.path
        inbox.close()
        with TelegramInbox(path) as restored:
            restored.recover_on_startup()
            assert [row["job_id"] for row in restored.active_jobs()] == [job["job_id"]]
            assert restored.active_job_page()["jobs"][0]["state"] == "waiting_control"
            assert restored.status()["active_jobs"] == 1
            if resume_before_recovery:
                restored.set_client_control(42, action="resume", expected_revision=1)
            assert restored.execution_slot()["job_id"] == job["job_id"]
            assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            app = wake.AppServer(wake.WakeConfig(parked["thread_id"]))
            app.connected = True
            app.ready_event.set()
            app._wait_turn = AsyncMock(return_value=None)
            app._turn_metadata = AsyncMock(return_value="completed")
            jobs = FakeJobs(restored)
            dispatcher = wake.DurableWakeDispatcher(app, 0, jobs)
            monkeypatch.setattr(dispatcher, "_use_thread", AsyncMock())
            await dispatcher._reconcile_active()
            app._wait_turn.assert_awaited_once()
            app._turn_metadata.assert_awaited_with(parked["turn_id"])
            assert restored.execution_slot() is None and dispatcher.stop_proof_verified
            assert restored.read_job(job["job_id"])["execution_proof"] == "completed"
            assert restored.active_jobs() == [] and restored.active_job_page()["jobs"] == []
            assert dispatcher.failed == 0
            if resume_before_recovery:
                # If the proof RPC response was lost, rereading the same
                # released job must still reconcile its exact old turn.
                app.active_turn = parked["turn_id"]
                app.active_event_id = event
                app.outcome_unknown = True
                await dispatcher._proof(parked, asyncio.get_running_loop().time() + 5)
                assert dispatcher.stop_proof_verified and not app.outcome_unknown
            if not resume_before_recovery:
                assert restored.read_job(job["job_id"])["state"] == "waiting_control"
                restored.set_client_control(42, action="resume", expected_revision=1)
            following = restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)
            assert following["event_ids"] == [event] and following["job_id"] != job["job_id"]

    asyncio.run(scenario())


async def _resolve(_client, peer):
    return int(peer), {"id": int(peer), "kind": "private"}


def test_completed_guidance_is_consumed_by_native_result_and_does_not_block_after_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", _resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        path = inbox.path
        incoming = _message(100)
        client = Client([incoming])
        try:
            monitor.record(incoming)
            job = _job(monitor)
            bridge._handle_client_control(
                {
                    "job_id": job["job_id"],
                    "action": "check_variant",
                    "text": "Option B",
                    "expected_control_revision": 0,
                },
                monitor,
            )
            job = inbox.refresh_job(**guard(job))
            request = {
                "operation": "send",
                "job_id": job["job_id"],
                "lease": job["lease"],
                "revision": job["revision"],
                "peer": "42",
                "text": "Synthetic option B result",
                "kind": "result",
                "reply_to_message_id": 100,
                "mode": "dry_run",
            }
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unread"):
                await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            state = await bridge._handle_job_operation(
                client, config, {"operation": "job_read", "job_id": job["job_id"]}, monitor
            )
            assert state["job"]["trusted_guidance"][0]["text"] == "Option B"
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            sent = await bridge._handle_operation(
                client,
                config,
                request
                | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
                inbound_monitor=monitor,
            )
            done = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_result",
                    **guard(job),
                    "outcome": "answered",
                    "event_ids": job["event_ids"],
                    "receipt_keys": [sent["receipt_key"]],
                },
                monitor,
            )
            close_turn(inbox, done["job"])
            assert inbox.client_control(42)["guidance_pending"] == 0
        finally:
            inbox.close()
        with TelegramInbox(path) as restored:
            monitor = bridge.DurableInboundMonitor(restored, enabled=True, config=config)
            monitor.self_peer_id = 900
            question = _message(102, text="Synthetic new request")
            client.messages[102] = question
            monitor.record(question)
            following = _job(monitor)
            dry = await bridge._handle_operation(
                client,
                config,
                {
                    "operation": "send",
                    "job_id": following["job_id"],
                    "lease": following["lease"],
                    "revision": following["revision"],
                    "peer": "42",
                    "text": "Synthetic new answer",
                    "kind": "result",
                    "reply_to_message_id": 102,
                    "mode": "dry_run",
                },
                inbound_monitor=monitor,
            )
            assert dry["ok"] is True and restored.client_control(42)["guidance_pending"] == 0

    asyncio.run(scenario())


def test_unread_guidance_cannot_be_consumed_by_terminal_result(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            incoming = _message(100)
            monitor.record(incoming)
            job = _job(monitor)
            bridge._handle_client_control(
                {"job_id": job["job_id"], "action": "context", "text": "New fact", "expected_control_revision": 0},
                monitor,
            )
            job = inbox.refresh_job(**guard(job))
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unread"):
                await bridge._handle_job_operation(
                    Client([incoming]),
                    config,
                    {
                        "operation": "job_result",
                        **guard(job),
                        "outcome": "no_reply_needed",
                        "event_ids": job["event_ids"],
                        "receipt_keys": [],
                        "reason_code": "not_actionable",
                    },
                    monitor,
                )
            assert inbox.client_control(42)["guidance_pending"] == 1
            await bridge._handle_job_operation(
                Client([incoming]), config, {"operation": "job_read", "job_id": job["job_id"]}, monitor
            )
            bridge._handle_client_control(
                {"job_id": job["job_id"], "action": "context", "text": "A newer fact", "expected_control_revision": 1},
                monitor,
            )
            job = inbox.refresh_job(**guard(job))
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unread"):
                await bridge._handle_job_operation(
                    Client([incoming]),
                    config,
                    {
                        "operation": "job_result",
                        **guard(job),
                        "outcome": "no_reply_needed",
                        "event_ids": job["event_ids"],
                        "receipt_keys": [],
                        "reason_code": "not_actionable",
                    },
                    monitor,
                )
            assert inbox.client_control(42)["revision"] == 2 and inbox.client_control(42)["guidance_pending"] == 1
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["result", "handoff"])
def test_pause_after_verified_delivery_finishes_truthfully_without_resend(tmp_path, monkeypatch, delivery):
    monkeypatch.setattr(bridge, "_resolve_peer", _resolve)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config, required=False: 20)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            source = _message(100)
            event = monitor.record(source)
            job = _job(monitor)
            client = Client([source])
            if delivery == "result":
                dry = await _send(client, config, monitor, job, kind="result", text="Synthetic complete answer")
                receipt = await _send(
                    client, config, monitor, job, kind="result", text="Synthetic complete answer", mode="apply", dry=dry
                )
            else:
                request = {
                    "operation": "job_handoff",
                    **guard(job),
                    "reply_to_message_id": 100,
                    "text": "Synthetic verified human handoff",
                    "mode": "dry_run",
                }
                dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
                receipt = await bridge._handle_operation(
                    client,
                    config,
                    request
                    | {
                        "mode": "apply",
                        "contract_token": dry["contract_token"],
                        "idempotency_key": dry["idempotency_key"],
                    },
                    inbound_monitor=monitor,
                )
            assert receipt["verified"] is True and client.sent == 1
            inbox.set_client_control(42, action="pause", expected_revision=0)
            with pytest.raises(InboxError, match="inbox_control_result_effects_present"):
                inbox.result(
                    **guard(job),
                    outcome="no_reply_needed",
                    event_ids=[event],
                    receipt_keys=[receipt["receipt_key"]],
                    reason_code="paused",
                )
            finished = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_result",
                    **guard(job),
                    "outcome": "no_reply_needed",
                    "event_ids": [event],
                    "receipt_keys": [receipt["receipt_key"]],
                    "reason_code": "already_answered",
                },
                monitor,
            )
            close_turn(inbox, finished["job"])
            assert finished["job"]["state"] == "completed" and client.sent == 1
            assert finished["job"]["effects"][0]["verified"] == 1
            inbox.set_client_control(42, action="resume", expected_revision=1)
            assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_human_answer_completion_batches_exact_reads_with_network_rpc_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", _resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        inbox = monitor.inbox
        try:
            incoming = _message(100)
            monitor.record(incoming)
            job = _job(monitor)
            human = [manual(identifier) for identifier in range(101, 151)]
            for message in human:
                monitor.record(message)
            job = inbox.refresh_job(**guard(job))

            class DelayedClient(Client):
                reads = 0

                async def get_messages(self, *args, **kwargs):
                    self.reads += 1
                    await asyncio.sleep(0.05)
                    return await super().get_messages(*args, **kwargs)

            client = DelayedClient([incoming, *human])
            request = {
                "operation": "job_result",
                **guard(job),
                "outcome": "no_reply_needed",
                "event_ids": job["event_ids"],
                "receipt_keys": [],
                "reason_code": "human_answered",
                "human_message_ids": list(range(101, 151)),
            }
            assert server_budget(request) == 20 and rpc_budget(request) == 30

            async def serve(reader, writer):
                await bridge._serve_client(client, config, reader, writer, inbound_monitor=monitor)

            async with await asyncio.start_unix_server(serve, path=tmp_path / "rpc.sock"):
                reader, writer = await asyncio.open_unix_connection(tmp_path / "rpc.sock")
                writer.write((json.dumps(request) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
            assert response["ok"] is True and response["job"]["state"] == "completed"
            assert client.reads == 1 and client.sent == 0
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_native_contact_service_never_enters_direct_history_or_presend_queue(tmp_path):
    tl = pytest.importorskip("telethon.tl.types")

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            service = tl.MessageService(
                id=99, peer_id=tl.PeerUser(42), date=datetime.now(UTC), action=tl.MessageActionContactSignUp()
            )
            assert getattr(service, "message", None) is None and monitor.record(service) is None

            class HistoryClient(Client):
                async def iter_messages(self, *_args, **_kwargs):
                    yield service

            client = HistoryClient([service])
            await bridge._reconcile_peer_history(client, 42, 42, monitor, 5)
            assert inbox.status()["open_events"] == 0 and inbox.checkpoint(42) == 99
            incoming = _message(100)
            client.messages[100] = incoming
            event = monitor.record(incoming)
            job = _job(monitor)
            dry = await _send(client, config, monitor, job, kind="result", text="Synthetic useful answer")
            assert dry["ok"] is True and inbox.read_job(job["job_id"])["event_ids"] == [event]
            assert inbox.status()["open_events"] == 1
        finally:
            inbox.close()

    asyncio.run(scenario())
