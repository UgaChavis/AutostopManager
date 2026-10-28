"""Native, isolated regressions for direct sends, trusted guidance and failed turns."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sqlite3

import pytest
from test_telegram_bridge_durable import Client, MediaClient, _job, _media_request, _message, _runtime, _send
from test_telegram_human_control import close_turn, guard, manual

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_inbox as inbox_module
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
    inbox._db.execute("ALTER TABLE client_controls DROP COLUMN guidance_count")
    inbox._db.execute("DROP INDEX jobs_effects_review_peer")
    inbox._db.execute("ALTER TABLE jobs DROP COLUMN effects_review_required")
    inbox._db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    inbox._db.commit()
    path = inbox.path
    inbox.close()
    with TelegramInbox(path) as restored:
        monitor = bridge.DurableInboundMonitor(restored, enabled=True)
        monitor.record(_message(102))
        assert restored.read_job(job["job_id"])["state"] == "needs_review"
        assert restored.read_job(job["job_id"])["reason_code"] == "failed_turn_effects_unreviewed"
        assert restored.read_job(job["job_id"])["effects"] == []
        assert restored.read_job(job["job_id"])["event_ids"] == [first]
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        monitor.record(_message(200, peer=43))
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)["peer_id"] == 43


@pytest.mark.parametrize("fresh_before_reopen", [False, True])
@pytest.mark.parametrize("schema_version", [4, 5])
def test_schema4_failed_orphan_with_completed_replacement_does_not_revive_on_migration(
    tmp_path, fresh_before_reopen, schema_version
):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    first = monitor.record(_message(100))
    original = _job(monitor)
    stopped = inbox.finish_execution(
        **guard(original),
        proof_kind="terminal",
        thread_id=original["thread_id"],
        turn_id=original["turn_id"],
        terminal_status="failed",
        local_tasks_done=True,
    )
    # The old schema4 recovery deferred started failures, and its next inbound
    # detached their open events. Reproduce only those two legacy mutations;
    # the replacement claim, verified receipt and completion use native APIs.
    inbox._db.execute(
        "UPDATE jobs SET state='failed_waiting_input',reason_code='retry_on_new_client_input' WHERE job_id=?",
        (stopped["job_id"],),
    )
    inbox._db.execute("UPDATE events SET job_id=NULL WHERE job_id=? AND state='open'", (stopped["job_id"],))
    inbox._db.commit()
    followup = monitor.record(_message(102))
    replacement = _job(monitor)
    assert set(replacement["event_ids"]) == {first, followup}
    receipt = inbox.reserve_effect(
        **guard(replacement), kind="result", text_sha256="a" * 64, peer_id=42, reply_to_message_id=100
    )
    inbox.mark_effect_intent(receipt)
    inbox.verify_effect(effect_key=receipt, receipt_key=receipt, message_id=103)
    completed = inbox.result(
        **guard(replacement), outcome="answered", event_ids=replacement["event_ids"], receipt_keys=[receipt]
    )
    close_turn(inbox, completed)
    historical = inbox.read_job(original["job_id"])
    replacement_before = inbox.read_job(replacement["job_id"])
    assert historical["event_ids"] == [] and historical["execution_proof"] == "failed"
    assert inbox.status()["open_events"] == 0 and inbox.status()["needs_review_jobs"] == 0
    fresh = monitor.record(_message(104)) if fresh_before_reopen else None
    path = close_as_schema4(inbox, schema_version=schema_version)
    with TelegramInbox(path) as restored:
        assert restored.status()["schema_version"] == 6
        assert restored.read_job(original["job_id"]) == historical
        assert restored.read_job(replacement["job_id"]) == replacement_before
        assert restored.event_state(first) == restored.event_state(followup) == "reply_verified"
        assert restored.status()["open_events"] == int(fresh_before_reopen)
        assert restored.status()["needs_review_jobs"] == 0
        assert restored._db.execute("SELECT COUNT(*) FROM jobs WHERE effects_review_required=1").fetchone()[0] == 0
        fresh = fresh or restored.record(42, 104)
        claimed = restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)
        assert claimed["peer_id"] == 42 and claimed["event_ids"] == [fresh]
        assert claimed["effects_review_required"] == 0


def legacy_failed_waiter(inbox, job):
    stopped = inbox.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status="failed",
        local_tasks_done=True,
    )
    # Emulate only old admission/intake mutations; every claim/start/proof below
    # uses current native APIs and does not depend on an old Git checkout.
    inbox._db.execute(
        "UPDATE jobs SET state='failed_waiting_input',reason_code='retry_on_new_client_input' WHERE job_id=?",
        (stopped["job_id"],),
    )
    inbox._db.execute("UPDATE events SET job_id=NULL WHERE job_id=? AND state='open'", (stopped["job_id"],))
    inbox._db.commit()
    return inbox.read_job(job["job_id"])


def close_as_schema4(inbox, *, schema_version=4):
    if schema_version == 4:
        inbox._db.execute("ALTER TABLE client_controls DROP COLUMN guidance_count")
    inbox._db.execute("DROP INDEX jobs_effects_review_peer")
    inbox._db.execute("ALTER TABLE jobs DROP COLUMN effects_review_required")
    inbox._db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(schema_version),))
    inbox._db.commit()
    path = inbox.path
    inbox.close()
    return path


@pytest.mark.parametrize(
    "phase", ["unassigned", "claimed", "start_intent", "running", "unknown_review", "verified_ack", "verified_result"]
)
@pytest.mark.parametrize("schema_version", [4, 5])
def test_schema4_detached_original_is_held_at_its_actual_owner_without_losing_execution(
    tmp_path, phase, schema_version
):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    first = monitor.record(_message(100))
    original = legacy_failed_waiter(inbox, _job(monitor))
    followup = monitor.record(_message(102))
    holder = None
    if phase != "unassigned":
        holder = _job(monitor, running=False)
        if phase != "claimed":
            holder = inbox.start_intent(**guard(holder), thread_id="test-thread")
        if phase in {"running", "unknown_review", "verified_ack", "verified_result"}:
            holder = inbox.bind_turn(**guard(holder), turn_id="replacement-turn")
        if phase in {"unknown_review", "verified_ack", "verified_result"}:
            effect = inbox.reserve_effect(
                **guard(holder),
                kind="ack" if phase == "verified_ack" else "result",
                text_sha256="a" * 64,
                peer_id=42,
                reply_to_message_id=100,
            )
            inbox.mark_effect_intent(effect)
            if phase == "unknown_review":
                holder = inbox.needs_review(**guard(holder), reason_code="send_outcome_uncertain")
            else:
                inbox.verify_effect(effect_key=effect, receipt_key=effect, message_id=103)
                holder = inbox.read_job(holder["job_id"])
    before = holder or original
    slot = tuple(inbox._db.execute("SELECT * FROM global_slot").fetchone() or ())
    path = close_as_schema4(inbox, schema_version=schema_version)
    with TelegramInbox(path) as restored:
        held = restored.read_job(before["job_id"])
        assert held["state"] == "needs_review" and held["effects_review_required"] == 1
        assert held["revision"] == before["revision"] + 1
        for key in ("lease", "thread_id", "turn_id", "execution_proof", "proof_at", "deadline_at", "effects"):
            assert held[key] == before[key]
        assert set(held["event_ids"]) == ({first} if holder is None else {first, followup})
        assert tuple(restored._db.execute("SELECT * FROM global_slot").fetchone() or ()) == slot
        assert held["reason_code"] == (
            "send_outcome_uncertain" if phase == "unknown_review" else "failed_turn_effects_unreviewed"
        )
        assert restored.read_job(original["job_id"])["state"] == ("needs_review" if holder is None else "retired")
        restored.recover_on_startup()
        assert restored.read_job(held["job_id"])["revision"] == held["revision"]
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        with pytest.raises(InboxError, match="inbox_resolution_review_required"):
            restored.execution_authority(**guard(held))
        with pytest.raises(InboxError, match="inbox_resolution_review_required"):
            restored.require_client_send_allowed(42)
        with pytest.raises(InboxError, match="inbox_resolution_review_required"):
            restored.start_intent(**guard(held), thread_id="test-thread")
        receipts = [effect["receipt_key"] for effect in held["effects"] if effect["verified"]]
        for outcome, reason in (
            ("no_reply_needed", "not_actionable"),
            ("no_reply_needed", "already_answered"),
            ("no_reply_needed", "paused"),
            ("answered", None),
        ):
            with pytest.raises(InboxError, match="inbox_resolution_review_required"):
                restored.result(
                    **guard(held),
                    outcome=outcome,
                    reason_code=reason,
                    event_ids=held["event_ids"],
                    receipt_keys=receipts,
                )
        if phase == "start_intent":
            bound = restored.bind_turn(**guard(held), turn_id="actual-late-turn")
            assert bound["state"] == "needs_review" and bound["effects_review_required"] == 1
            assert bound["reason_code"] == held["reason_code"]
            assert bound["lease"] == held["lease"] and bound["turn_id"] == "actual-late-turn"
            with pytest.raises(InboxError, match="inbox_resolution_review_required"):
                restored.execution_authority(**guard(bound))
        assert all(job["event_ids"] for job in restored.active_jobs())
        saved = restored.read_job(held["job_id"])
    with TelegramInbox(path) as reopened:
        assert reopened.read_job(saved["job_id"]) == saved
        assert reopened.event_state(first) == reopened.event_state(followup) == "open"


@pytest.mark.parametrize("phase", ["claimed", "start_intent"])
def test_migrated_original_requires_explicit_review_even_after_not_started_reason_change_and_reopen(tmp_path, phase):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    first = monitor.record(_message(100))
    original = legacy_failed_waiter(inbox, _job(monitor))
    second = monitor.record(_message(102))
    holder = _job(monitor, running=False)
    if phase == "start_intent":
        holder = inbox.start_intent(**guard(holder), thread_id="test-thread")
    path = close_as_schema4(inbox)
    with TelegramInbox(path) as restored:
        held = restored.read_job(holder["job_id"])
        stopped = restored.finish_execution(
            **guard(held),
            proof_kind="not_started" if phase == "claimed" else "start_rejected",
            **({"thread_id": "test-thread"} if phase == "start_intent" else {}),
            local_tasks_done=True,
        )
        changed = restored.needs_review(**guard(stopped), reason_code="codex_turn_start_rejected")
        assert changed["execution_proof"] == "not_started" and changed["effects_review_required"] == 1
        with pytest.raises(InboxError, match="inbox_failure_recovery_unsafe"):
            restored.defer_failed_request(**guard(changed))
    with TelegramInbox(path) as reopened:
        held = reopened.read_job(changed["job_id"])
        assert held == changed and reopened.read_job(original["job_id"])["state"] == "retired"
        repeated = reopened.finish_execution(
            **guard(held),
            proof_kind="not_started" if phase == "claimed" else "start_rejected",
            **({"thread_id": "test-thread"} if phase == "start_intent" else {}),
            local_tasks_done=True,
        )
        assert repeated == held
        with pytest.raises(InboxError, match="inbox_failure_recovery_unsafe"):
            reopened.defer_failed_request(**guard(held))
        reopened.set_execution_paused(paused=True, expected_revision=reopened.status()["execution_revision"])
        with pytest.raises(InboxError, match="inbox_resolution_review_required"):
            reopened.resolve_job(**guard(held), decision="retry_no_effects", effects_reviewed=False)
        reviewed = reopened.resolve_job(**guard(held), decision="retry_no_effects", effects_reviewed=True)
        assert reviewed["state"] == "released" and reviewed["effects_review_required"] == 0
        reopened.set_execution_paused(paused=False, expected_revision=reopened.status()["execution_revision"])
    with TelegramInbox(path) as recovered:
        claimed = recovered.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)
        assert claimed["peer_id"] == 42 and set(claimed["event_ids"]) == {first, second}
        assert recovered.read_job(original["job_id"])["state"] == "retired"


def test_multiple_legacy_failed_fences_keep_one_valid_review_owner_and_all_open_events(tmp_path):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    first = monitor.record(_message(100))
    oldest = legacy_failed_waiter(inbox, _job(monitor))
    second = monitor.record(_message(102))
    newest = legacy_failed_waiter(inbox, _job(monitor))
    third = monitor.record(_message(104))
    path = close_as_schema4(inbox)
    with TelegramInbox(path) as restored:
        held = restored.read_job(newest["job_id"])
        assert held["state"] == "needs_review" and set(held["event_ids"]) == {first, second}
        assert held["lease"] == newest["lease"] and held["execution_proof"] == "failed"
        assert restored.read_job(oldest["job_id"])["state"] == "retired"
        assert restored.read_job(oldest["job_id"])["execution_proof"] == "failed"
        assert restored.status()["open_events"] == 3 and restored.event_state(third) == "open"
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        assert [job["job_id"] for job in restored.active_jobs()] == [newest["job_id"]]


def test_retired_failed_fence_preserves_verified_ack_receipts_and_agent_attribution(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config, monitor.self_peer_id = config, 900
        inbox = monitor.inbox
        incoming = _message(100)
        monitor.record(incoming)
        original = _job(monitor)
        client = Client([incoming])
        dry = await _send(client, config, monitor, original, kind="ack", text="Synthetic acknowledgement")
        ack = await _send(
            client, config, monitor, original, kind="ack", text="Synthetic acknowledgement", mode="apply", dry=dry
        )
        original = legacy_failed_waiter(inbox, original)
        monitor.record(_message(102))
        holder = _job(monitor, running=False)
        prior = holder["prior_acknowledgements"]
        assert prior[0]["receipt_key"] == ack["receipt_key"]
        ledger = (config.state_dir / "idempotency.json").read_bytes()
        path = close_as_schema4(inbox)
        with TelegramInbox(path) as restored:
            old = restored.read_job(original["job_id"])
            held = restored.read_job(holder["job_id"])
            assert old["state"] == "retired" and old["effects"] == original["effects"]
            assert old["lease"] == original["lease"] and old["execution_proof"] == "failed"
            assert held["prior_acknowledgements"] == prior and held["effects_review_required"] == 1
            assert (config.state_dir / "idempotency.json").read_bytes() == ledger
            assert bridge._native_outgoing_origin(config, 42, ack["message_id"]) == "agent"
            monitor = bridge.DurableInboundMonitor(restored, enabled=True, config=config)
            monitor.self_peer_id = 900
            assert monitor.observe_self_message(manual(ack["message_id"])) == "agent"
            assert client.sent == 1

    asyncio.run(scenario())


def test_explicit_review_continues_bound_migrated_turn_without_reintroducing_old_fence(tmp_path):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    monitor.record(_message(100))
    old = legacy_failed_waiter(inbox, _job(monitor))
    monitor.record(_message(102))
    holder = _job(monitor)
    path = close_as_schema4(inbox)
    with TelegramInbox(path) as restored:
        held = restored.read_job(holder["job_id"])
        stopped = restored.finish_execution(
            **guard(held),
            proof_kind="terminal",
            thread_id=held["thread_id"],
            turn_id=held["turn_id"],
            terminal_status="failed",
            local_tasks_done=True,
        )
        restored.set_execution_paused(paused=True, expected_revision=restored.status()["execution_revision"])
        reviewed = restored.resolve_job(**guard(stopped), decision="continue_bound_turn", effects_reviewed=True)
        assert reviewed["effects_review_required"] == 0 and reviewed["manual_recovery"] == 1
        restored.execution_authority(**guard(reviewed))
    with TelegramInbox(path) as reopened:
        assert reopened.read_job(old["job_id"])["state"] == "retired"
        assert reopened.read_job(reviewed["job_id"]) == reviewed
        reopened.execution_authority(**guard(reviewed))


@pytest.mark.parametrize("phase", ["claimed", "start_intent"])
def test_actual_dispatcher_rpc_keeps_migrated_peer_review_without_infrastructure_hold(tmp_path, phase):
    if os.geteuid() != 0:
        pytest.skip("the native work bridge IPC requires root")

    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        monitor.record(_message(100))
        legacy_failed_waiter(inbox, _job(monitor))
        monitor.record(_message(102))
        holder = _job(monitor, running=False)
        if phase == "start_intent":
            holder = inbox.start_intent(**guard(holder), thread_id="test-thread")
        path = close_as_schema4(inbox)
        with TelegramInbox(path) as restored:
            monitor = bridge.DurableInboundMonitor(restored, enabled=True, config=config)
            client = Client([_message(100), _message(102)])

            async def serve(reader, writer):
                await bridge._serve_client(client, config, reader, writer, inbound_monitor=monitor)

            class RecordingJobs(wake.BridgeJobs):
                def __init__(self):
                    super().__init__(os.geteuid(), path=config.socket_path)
                    self.calls = []

                async def request(self, operation, **kwargs):
                    self.calls.append(operation)
                    return await super().request(operation, **kwargs)

            async with await asyncio.start_unix_server(serve, path=config.socket_path):
                jobs = RecordingJobs()
                app = wake.AppServer(wake.WakeConfig("test-thread"))
                app.connected = True
                app.ready_event.set()
                dispatcher = wake.DurableWakeDispatcher(app, 0, jobs)
                held = restored.read_job(holder["job_id"])
                try:
                    code = "codex_turn_failed" if phase == "claimed" else "codex_turn_start_rejected"
                    await dispatcher._finish_request(held, code)
                    stopped = restored.read_job(held["job_id"])
                    assert stopped["state"] == "needs_review" and stopped["effects_review_required"] == 1
                    assert stopped["execution_proof"] == "not_started" and restored.execution_slot() is None
                    assert "job_execution_proof" in jobs.calls and "job_defer_failed" not in jobs.calls
                    assert dispatcher.execution_error is None and not dispatcher.terminal_stop_pending
                    assert restored.status()["execution_paused"] is False
                    claim = {
                        "instruction_root": "/test/snapshot",
                        "instruction_revision": "a" * 40,
                        "worker_protocol": 2,
                    }
                    assert (await jobs.request("job_claim", **claim))["job"] is None
                    restored.record(43, 200)
                    assert (await jobs.request("job_claim", **claim))["job"]["peer_id"] == 43
                finally:
                    await jobs.close()

    asyncio.run(scenario())


def test_schema5_existing_unknown_hold_keeps_its_facts_and_gets_safe_default_marker(tmp_path):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    event = monitor.record(_message(100))
    job = _job(monitor)
    effect = inbox.reserve_effect(
        **guard(job), kind="result", text_sha256="a" * 64, peer_id=42, reply_to_message_id=100
    )
    inbox.mark_effect_intent(effect)
    job = inbox.needs_review(**guard(job), reason_code="send_outcome_uncertain")
    before = inbox.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status="failed",
        local_tasks_done=True,
    )
    path = close_as_schema4(inbox, schema_version=5)
    with TelegramInbox(path) as restored:
        assert restored.status()["schema_version"] == 6
        assert restored.read_job(job["job_id"]) == before
        assert before["effects_review_required"] == 0
        assert restored.event_state(event) == "open"
        restored.record(42, 102)
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        restored.record(43, 200)
        assert restored.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)["peer_id"] == 43


def test_schema6_review_marker_is_rejected_by_schema5_writer_before_mutation(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    inbox = monitor.inbox
    monitor.record(_message(100))
    old = legacy_failed_waiter(inbox, _job(monitor))
    monitor.record(_message(102))
    path = close_as_schema4(inbox, schema_version=5)
    with TelegramInbox(path) as current:
        assert current._meta("schema_version") == "6"
        assert current.read_job(old["job_id"])["effects_review_required"] == 1
        before = list(current._db.iterdump())
    # The version check is the old writer's unchanged native admission gate;
    # no old checkout or Git access is needed in CI.
    monkeypatch.setattr(inbox_module, "SUPPORTED_INBOX_SCHEMA_VERSIONS", (1, 2, 3, 4, 5))
    with pytest.raises(InboxError, match="inbox_open_failed"):
        TelegramInbox(path)
    with sqlite3.connect(path) as unchanged:
        assert list(unchanged.iterdump()) == before


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
