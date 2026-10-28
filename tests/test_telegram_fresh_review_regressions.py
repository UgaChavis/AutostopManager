"""Native, isolated regressions for direct sends, trusted guidance and failed turns."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest
from test_telegram_bridge_durable import Client, MediaClient, _job, _media_request, _message, _runtime
from test_telegram_human_control import close_turn, guard, manual

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_effects import EffectExecutor
from autostop_manager.telegram_inbox import InboxError, TelegramInbox
from test_telegram_wake_durable import FakeJobs


async def resolve(_client, _peer):
    return 42, {"id": 42, "kind": "private"}


def direct_request(config, operation):
    request = (
        {"operation": "send", "peer": "42", "text": "Synthetic answer", "mode": "dry_run"}
        if operation == "send"
        else _media_request(config, operation)
    )
    return request | {
        "peer": "42" if operation == "send_document" else "@synthetic_client",
        "idempotency_key": "synthetic-direct",
    }


def control(monitor, action, text=None):
    revision = monitor.inbox.client_control(42)["revision"]
    return bridge._handle_client_control(
        {"peer_id": 42, "action": action, "expected_control_revision": revision, **({"text": text} if text else {})},
        monitor,
    )


async def send_result(client, config, monitor, job):
    request = {
        "operation": "send",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "peer": "42",
        "kind": "result",
        "reply_to_message_id": 100,
        "text": "Synthetic checked result",
        "mode": "dry_run",
    }
    dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
    return await bridge._handle_operation(
        client,
        config,
        request | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
        inbound_monitor=monitor,
    )


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
@pytest.mark.parametrize("hold", ["pause", "takeover", "native_unknown", "inbox_unknown"])
def test_direct_resolved_peer_send_obeys_hold_at_dry_run_and_apply(tmp_path, monkeypatch, operation, hold):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config = config
        inbox = monitor.inbox
        try:
            incoming = _message(100)
            monitor.record(incoming)
            job = _job(monitor)
            client = MediaClient([incoming])
            request = direct_request(config, operation)
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            ledger = config.state_dir / "idempotency.json"
            if hold in {"pause", "takeover"}:
                control(monitor, hold)
                expected = "inbox_client_" + ("paused" if hold == "pause" else "human_takeover")
            elif hold == "native_unknown":
                bridge._save_idempotency(
                    ledger,
                    {
                        "original": {
                            "operation": "send_photo",
                            "peer_id": 42,
                            "message_id": 0,
                            "readback_verified": False,
                        }
                    },
                )
                expected = "send_outcome_uncertain"
            else:
                effect = inbox.reserve_effect(
                    **guard(job), kind="result", text_sha256="a" * 64, peer_id=42, reply_to_message_id=100
                )
                inbox.mark_effect_intent(effect)
                expected = "inbox_effect_outcome_unknown"
            before = bridge._load_idempotency(ledger)
            for attempt in (request, request | {"mode": "apply", "contract_token": dry["contract_token"]}):
                with pytest.raises((InboxError, bridge.BridgeError), match=expected):
                    await bridge._handle_operation(client, config, attempt, inbound_monitor=monitor)
                assert client.sent == 0 and bridge._load_idempotency(ledger) == before
            assert inbox.read_job(job["job_id"])["state"] == "running"
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
def test_pause_during_direct_transport_read_prevents_new_intent(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        try:
            incoming = _message(100)
            monitor.record(incoming)
            client = MediaClient([incoming])
            request = direct_request(config, operation)
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            original = bridge._last_message_id

            async def late_pause(*args):
                result = await original(*args)
                control(monitor, "pause")
                return result

            monkeypatch.setattr(bridge, "_last_message_id", late_pause)
            with pytest.raises(InboxError, match="inbox_client_paused"):
                await bridge._handle_operation(
                    client,
                    config,
                    request | {"mode": "apply", "contract_token": dry["contract_token"]},
                    inbound_monitor=monitor,
                )
            assert client.sent == 0 and bridge._load_idempotency(config.state_dir / "idempotency.json") == {}
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_direct_request_queued_before_pause_rechecks_after_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        executor = EffectExecutor()
        release = asyncio.Event()
        try:
            monitor.record(_message(100))
            client = Client([_message(100)])
            request = direct_request(config, "send")
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            blocker = asyncio.create_task(executor.submit(release.wait, timeout=2))
            await asyncio.sleep(0)
            queued = asyncio.create_task(
                executor.submit(
                    lambda: bridge._handle_operation(
                        client,
                        config,
                        request | {"mode": "apply", "contract_token": dry["contract_token"]},
                        inbound_monitor=monitor,
                    ),
                    timeout=2,
                )
            )
            await asyncio.sleep(0)
            assert executor.queued == 1
            control(monitor, "pause")
            release.set()
            await blocker
            with pytest.raises(InboxError, match="inbox_client_paused"):
                await queued
            assert client.sent == 0 and bridge._load_idempotency(config.state_dir / "idempotency.json") == {}
        finally:
            release.set()
            await executor.close()
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
@pytest.mark.parametrize("verified", [False, True])
def test_direct_original_key_readback_survives_late_pause_without_new_send(tmp_path, monkeypatch, operation, verified):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        try:
            monitor.record(_message(100))
            client = MediaClient([_message(100)])
            request = direct_request(config, operation)
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            apply = request | {"mode": "apply", "contract_token": dry["contract_token"]}
            sent = await bridge._handle_operation(client, config, apply, inbound_monitor=monitor)
            path = config.state_dir / "idempotency.json"
            entries = bridge._load_idempotency(path)
            entries[request["idempotency_key"]]["readback_verified"] = verified
            bridge._save_idempotency(path, entries)
            control(monitor, "pause")
            replay = await bridge._handle_operation(client, config, apply, inbound_monitor=monitor)
            assert replay["verified"] and replay["replayed"] and replay["message_id"] == sent["message_id"]
            assert client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action,reason", [("pause", "paused"), ("takeover", "human_takeover")])
@pytest.mark.parametrize("hint", ["check_variant", "context", "reconsider"])
def test_unread_guidance_survives_park_resume_until_read_and_consumed(tmp_path, monkeypatch, action, reason, hint):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config = config
        inbox = monitor.inbox
        try:
            monitor.record(_message(100))
            job = _job(monitor)
            client = Client([_message(100)])
            control(monitor, hint, "Check independent option before replying")
            control(monitor, action)
            parked = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_result",
                    **guard(job),
                    "outcome": "no_reply_needed",
                    "reason_code": reason,
                    "event_ids": job["event_ids"],
                    "receipt_keys": [],
                },
                monitor,
            )
            assert inbox.client_control(42)["guidance_count"] == 1
            close_turn(inbox, parked["job"])
            control(monitor, "resume")
            resumed = _job(monitor)
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unread"):
                await send_result(client, config, monitor, resumed)
            snapshot = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_read",
                    "job_id": resumed["job_id"],
                },
                monitor,
            )
            assert [note["revision"] for note in snapshot["job"]["trusted_guidance"]] == [1]
            receipt = await send_result(client, config, monitor, resumed)
            done = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_result",
                    **guard(resumed),
                    "outcome": "answered",
                    "event_ids": resumed["event_ids"],
                    "receipt_keys": [receipt["receipt_key"]],
                },
                monitor,
            )
            assert done["job"]["state"] == "completed" and client.sent == 1
            assert inbox.client_control(42)["guidance_count"] == 0
            assert monitor.trusted_guidance.get(42, []) == []
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_guidance_capacity_rejects_before_mutation_and_preserves_every_unread_note(tmp_path):
    _config, monitor = _runtime(tmp_path)
    try:
        monitor.record(_message(100))
        for index in range(32):
            control(monitor, "check_variant", f"Independent option {index}")
        previous = monitor.inbox.client_control(42)
        notes = list(monitor.trusted_guidance[42])
        with pytest.raises(InboxError, match="inbox_client_guidance_full"):
            control(monitor, "context", "The overflow must be rejected")
        assert monitor.inbox.client_control(42) == previous
        assert monitor.trusted_guidance[42] == notes
        assert [note["revision"] for note in notes] == list(range(1, 33))
        with pytest.raises(bridge.BridgeError, match="inbox_guidance_recovery_unneeded"):
            control(monitor, "recover_guidance", "Do not replace available instructions")
    finally:
        monitor.inbox.close()


def test_lost_guidance_full_queue_stays_held_on_resume_and_supports_explicit_restatement(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        monitor.record(_message(100))
        for index in range(32):
            control(monitor, "context", f"Synthetic fact {index}")
        control(monitor, "pause")
        path = inbox.path
        inbox.close()
        with TelegramInbox(path) as restored:
            monitor = bridge.DurableInboundMonitor(restored, enabled=True, config=config)
            control(monitor, "resume")
            job = _job(monitor)
            client = Client([_message(100)])
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unavailable"):
                await send_result(client, config, monitor, job)
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unavailable"):
                await bridge._handle_operation(client, config, direct_request(config, "send"), inbound_monitor=monitor)
            with pytest.raises(InboxError, match="inbox_client_guidance_full"):
                control(monitor, "context", "Appending does not recover missing instructions")
            args = bridge.build_parser().parse_args(
                [
                    "--account",
                    "work",
                    "job-client-control",
                    "--peer-id",
                    "42",
                    "--action",
                    "recover_guidance",
                    "--expected-control-revision",
                    str(restored.client_control(42)["revision"]),
                    "--text",
                    "Complete manually restored instruction set",
                ]
            )
            bridge._handle_client_control(bridge._job_cli_request(args), monitor)
            assert restored.client_control(42)["guidance_count"] == 1
            job = restored.refresh_job(**guard(job))
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unread"):
                await send_result(client, config, monitor, job)
            snapshot = await bridge._handle_job_operation(
                client, config, {"operation": "job_read", "job_id": job["job_id"]}, monitor
            )
            assert len(snapshot["job"]["trusted_guidance"]) == 1
            assert (await send_result(client, config, monitor, job))["verified"]

    asyncio.run(scenario())


def test_verified_former_owner_history_does_not_block_new_owner_or_manual_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config, required=False: 42)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config, monitor.self_peer_id = config, 900
        inbox = monitor.inbox
        try:
            key = bridge._ensure_private_key(config.state_dir / "contract.key")
            digest = hmac.new(key, b"owner\x0041", hashlib.sha256).digest()
            old = {
                "operation": "send_owner_notification",
                "target_role": "owner",
                "message_id": 90,
                "target_fingerprint": f"{(int.from_bytes(digest[:8], 'big') or 1):016x}",
                "readback_verified": True,
            }
            path = config.state_dir / "idempotency.json"
            bridge._save_idempotency(path, {"old-owner": old})
            assert bridge._native_outgoing_origin(config, 41, 90) == "agent"
            assert bridge._native_outgoing_origin(config, 42, -1) is None
            assert monitor.observe_self_message(manual(99)) == "human"
            monitor.record(_message(100))
            job = _job(monitor)
            assert (await send_result(Client([_message(100)]), config, monitor, job))["verified"]
            entries = bridge._load_idempotency(path)
            entries["old-owner"] = old | {"message_id": 0, "readback_verified": False}
            bridge._save_idempotency(path, entries)
            assert bridge._native_outgoing_origin(config, 41, -1) == "unresolved"
            assert bridge._native_outgoing_origin(config, 42, -1) is None
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
def test_direct_unknown_original_key_stays_uncertain_after_pause(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        try:
            monitor.record(_message(100))
            client = MediaClient([_message(100)])
            request = direct_request(config, operation)
            dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            apply = request | {"mode": "apply", "contract_token": dry["contract_token"]}
            client.fail_send = True
            with pytest.raises(OSError):
                await bridge._handle_operation(client, config, apply, inbound_monitor=monitor)
            path = config.state_dir / "idempotency.json"
            unknown = bridge._load_idempotency(path)
            control(monitor, "pause")
            with pytest.raises(bridge.BridgeError, match="outcome_uncertain"):
                await bridge._handle_operation(client, config, apply, inbound_monitor=monitor)
            assert client.sent == 1 and bridge._load_idempotency(path) == unknown
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_guidance_metadata_migration_and_new_note_cannot_hide_lost_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        monitor.record(_message(100))
        control(monitor, "context", "Lost synthetic instruction")
        inbox._db.execute("ALTER TABLE client_controls DROP COLUMN guidance_count")
        inbox._db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
        inbox._db.commit()
        path = inbox.path
        inbox.close()
        with TelegramInbox(path) as restored:
            monitor = bridge.DurableInboundMonitor(restored, enabled=True, config=config)
            assert restored.client_control(42)["guidance_count"] == 1
            control(monitor, "context", "A new fact does not replace the unavailable earlier instruction")
            job = _job(monitor)
            client = Client([_message(100)])
            await bridge._handle_job_operation(
                client, config, {"operation": "job_read", "job_id": job["job_id"]}, monitor
            )
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unavailable"):
                await send_result(client, config, monitor, job)
            assert client.sent == 0 and restored.client_control(42)["guidance_count"] == 2

    asyncio.run(scenario())


def test_legacy_unsafe_failed_waiter_reopen_requires_review(tmp_path):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    first = monitor.record(_message(100))
    job = _job(monitor)
    stopped = inbox.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status="failed",
        local_tasks_done=True,
    )
    inbox._db.execute("UPDATE jobs SET state='failed_waiting_input' WHERE job_id=?", (stopped["job_id"],))
    inbox._db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    inbox._db.commit()
    path = inbox.path
    inbox.close()
    with TelegramInbox(path) as restored:
        monitor = bridge.DurableInboundMonitor(restored, enabled=True)
        monitor.record(_message(102))
        assert restored.read_job(job["job_id"])["state"] == "needs_review"
        assert restored.read_job(job["job_id"])["event_ids"] == [first]
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None


def test_failed_dispatch_before_any_turn_keeps_safe_not_started_recovery(tmp_path):
    async def scenario():
        _config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            first = monitor.record(_message(100))
            job = _job(monitor, running=False)
            app = wake.AppServer(wake.WakeConfig("test-thread"))
            app.connected = True
            app.ready_event.set()
            jobs = FakeJobs(inbox)
            dispatcher = wake.DurableWakeDispatcher(app, 0, jobs)
            await dispatcher._finish_request(job, "codex_turn_failed")
            stopped = inbox.read_job(job["job_id"])
            assert stopped["execution_proof"] == "not_started"
            assert stopped["state"] == "failed_waiting_input"
            assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            second = monitor.record(_message(102))
            assert set(_job(monitor)["event_ids"]) == {first, second}
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", [None, "ack", "result_without_receipt", "result"])
@pytest.mark.parametrize("lost_guidance", [False, True])
def test_already_answered_requires_supplied_verified_answer_not_ack_or_claim(
    tmp_path, monkeypatch, delivery, lost_guidance
):
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config = config
        inbox = monitor.inbox
        try:
            incoming = _message(100)
            event = monitor.record(incoming)
            job = _job(monitor)
            client = Client([incoming])
            receipt_keys = []
            if delivery:
                request = {
                    "operation": "send",
                    "peer": "42",
                    "text": "Synthetic delivered message",
                    "job_id": job["job_id"],
                    "lease": job["lease"],
                    "revision": job["revision"],
                    "kind": "ack" if delivery == "ack" else "result",
                    "reply_to_message_id": 100,
                    "mode": "dry_run",
                }
                dry = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
                sent = await bridge._handle_operation(
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
                if delivery != "result_without_receipt":
                    receipt_keys = [sent["receipt_key"]]
            if lost_guidance:
                control(monitor, "context", "Synthetic outstanding mandatory check")
                monitor.trusted_guidance.clear()
            control(monitor, "pause")
            request = {
                "operation": "job_result",
                **guard(job),
                "outcome": "no_reply_needed",
                "reason_code": "already_answered",
                "event_ids": [event],
                "receipt_keys": receipt_keys,
            }
            if delivery == "result":
                finished = await bridge._handle_job_operation(client, config, request, monitor)
                assert finished["job"]["state"] == "completed"
                assert inbox.event_state(event) == "no_reply_needed"
                assert inbox.client_control(42)["guidance_pending"] == int(lost_guidance)
                assert client.sent == 1
            else:
                expected = "inbox_guidance_unavailable" if lost_guidance else "inbox_result_receipt_required"
                with pytest.raises((bridge.BridgeError, InboxError), match=expected):
                    await bridge._handle_job_operation(client, config, request, monitor)
                # The durable ledger also rejects the false completion if a
                # native caller bypasses the transient guidance gate entirely.
                with pytest.raises(InboxError, match="inbox_result_receipt_required"):
                    inbox.result(
                        **guard(job),
                        outcome="no_reply_needed",
                        reason_code="already_answered",
                        event_ids=[event],
                        receipt_keys=receipt_keys,
                    )
                assert inbox.read_job(job["job_id"])["state"] == "running"
                assert inbox.event_state(event) == "open"
                assert client.sent == int(bool(delivery))
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("parked", [False, True])
def test_started_failed_turn_remains_bound_for_cross_system_review_even_without_telegram_effects(tmp_path, parked):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    try:
        first = monitor.record(_message(100))
        job = _job(monitor)
        if parked:
            control(monitor, "pause")
            job = inbox.result(
                **guard(job), outcome="no_reply_needed", event_ids=[first], receipt_keys=[], reason_code="paused"
            )
        stopped = inbox.finish_execution(
            **guard(job),
            proof_kind="terminal",
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            terminal_status="failed",
            local_tasks_done=True,
        )
        assert stopped["state"] == "needs_review" and stopped["effects"] == []
        with pytest.raises(InboxError, match="inbox_failure_recovery_unsafe"):
            inbox.defer_failed_request(**guard(stopped))
        if parked:
            control(monitor, "resume")
        second = monitor.record(_message(102))
        assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        assert inbox.read_job(job["job_id"])["event_ids"] == [first]
        assert inbox.event_state(first) == inbox.event_state(second) == "open"
        inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
        recovered = inbox.resolve_job(**guard(stopped), decision="continue_bound_turn", effects_reviewed=True)
        assert recovered["manual_recovery"] and recovered["event_ids"] == [first]
        assert recovered["job_id"] == job["job_id"]
    finally:
        inbox.close()
