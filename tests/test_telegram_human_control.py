"""Synthetic client conversations with manual work-account participation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from types import SimpleNamespace

import pytest
from test_telegram_bridge_durable import Client, _job, _message, _runtime, _send

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


def manual(identifier, *, peer=42, text="Ответ человека", sender=900):
    message = _message(identifier, peer=peer, out=True, text=text)
    message.sender_id = sender
    return message


def close_turn(inbox, job):
    return inbox.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status="completed",
        local_tasks_done=True,
    )


def test_contact_greeting_reaction_then_request_and_duplicate(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        inbox = monitor.inbox
        try:
            service = _message(99, text="")
            service.message = SimpleNamespace(action=object(), media=None)
            assert monitor.record(service) is None
            assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            hello = _message(100, text="Здравствуйте")
            first = monitor.record(hello)
            job = _job(monitor)
            client = Client([hello])
            dry = await _send(client, config, monitor, job, kind="result", text="Здравствуйте! Чем помочь?")
            sent = await _send(
                client, config, monitor, job, kind="result", text="Здравствуйте! Чем помочь?", mode="apply", dry=dry
            )
            done = inbox.result(
                **guard(job), outcome="answered", event_ids=job["event_ids"], receipt_keys=[sent["receipt_key"]]
            )
            close_turn(inbox, done)
            assert monitor.record(hello) == first and client.sent == 1
            smile = _message(102, text="👍")
            monitor.record(smile)
            reaction = _job(monitor)
            done = inbox.result(
                **guard(reaction),
                outcome="no_reply_needed",
                event_ids=reaction["event_ids"],
                receipt_keys=[],
                reason_code="reaction_only",
            )
            close_turn(inbox, done)
            substantive = _message(103, text="Нужны колодки и стоимость замены")
            monitor.record(substantive)
            next_job = _job(monitor)
            assert next_job["peer_id"] == 42 and len(next_job["event_ids"]) == 1
            assert inbox.status()["open_events"] == 1
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_human_answer_invalidates_draft_and_finishes_without_agent_receipt(tmp_path, monkeypatch):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        inbox = monitor.inbox
        try:
            incoming = _message(100)
            monitor.record(incoming)
            job = _job(monitor)
            client = Client([incoming])
            await _send(client, config, monitor, job, kind="result", text="Устаревший черновик")
            human = manual(101)
            client.messages[101] = human
            assert monitor.record(human) is None
            assert inbox.status()["open_events"] == 1
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await _send(client, config, monitor, job, kind="result", text="Устаревший черновик")
            job = inbox.refresh_job(**guard(job))

            async def resolve(_client, peer):
                return int(peer), {"id": int(peer), "kind": "private"}

            monkeypatch.setattr(bridge, "_resolve_peer", resolve)
            done = await bridge._handle_job_operation(
                client,
                config,
                {
                    "operation": "job_result",
                    **guard(job),
                    "outcome": "no_reply_needed",
                    "event_ids": job["event_ids"],
                    "receipt_keys": [],
                    "reason_code": "human_answered",
                    "human_message_ids": [101],
                },
                monitor,
            )
            assert done["job"]["effects"] == [] and client.sent == 0
            close_turn(inbox, done["job"])
            monitor.record(_message(102, text="А можно записаться?"))
            assert _job(monitor)["state"] == "running"
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_partial_human_answer_allows_only_refreshed_remaining_response(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        inbox = monitor.inbox
        try:
            incoming = _message(100, text="Нужны детали и работа")
            monitor.record(incoming)
            job = _job(monitor)
            client = Client([incoming])
            dry = await _send(client, config, monitor, job, kind="result", text="Детали и работа")
            client.messages[101] = manual(101, text="Детали стоят 5000 рублей")
            # Discover the update through current transport history even if the
            # event callback has not yet run.
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await _send(client, config, monitor, job, kind="result", text="Детали и работа", mode="apply", dry=dry)
            job = inbox.refresh_job(**guard(job))
            dry = await _send(client, config, monitor, job, kind="result", text="Работу уточним после диагностики")
            sent = await _send(
                client,
                config,
                monitor,
                job,
                kind="result",
                text="Работу уточним после диагностики",
                mode="apply",
                dry=dry,
            )
            assert client.sent == 1 and sent["verified"] is True
            assert inbox.read_job(job["job_id"])["state"] == "running"
            assert monitor.observe_self_message(manual(101)) == "human"
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_unknown_agent_send_never_becomes_human_merely_for_lack_of_receipt(tmp_path):
    config, monitor = _runtime(tmp_path)
    del config
    monitor.self_peer_id = 900
    inbox = monitor.inbox
    try:
        monitor.record(_message(100))
        job = _job(monitor)
        key = inbox.reserve_effect(
            **guard(job), kind="result", text_sha256="b" * 64, peer_id=42, reply_to_message_id=100
        )
        inbox.mark_effect_intent(key)
        assert monitor.observe_self_message(manual(101)) == "unresolved"
        assert monitor.observe_self_message(manual(102)) == "unresolved"
        with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
            inbox.result(
                **guard(job),
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[],
                reason_code="human_answered",
                human_message_ids=[101],
            )
        # Models the exact original send's independently checked receipt.
        inbox.verify_effect(effect_key=key, receipt_key=key, message_id=101)
        monitor.reconcile_outgoing(42)
        assert inbox.outgoing_context(42) == [
            {"message_id": 102, "origin": "human"},
            {"message_id": 101, "origin": "agent"},
        ]
        job = inbox.refresh_job(**guard(job))
        done = inbox.result(
            **guard(job),
            outcome="no_reply_needed",
            event_ids=job["event_ids"],
            receipt_keys=[key],
            reason_code="human_answered",
            human_message_ids=[102],
        )
        assert len(done["effects"]) == 1
        assert done["effects"][0]["sent_message_id"] == 101
    finally:
        inbox.close()


def test_transport_identity_own_outgoing_has_no_loop_or_false_context_change(tmp_path):
    config, monitor = _runtime(tmp_path)
    del config
    monitor.self_peer_id = 900
    inbox = monitor.inbox
    try:
        monitor.record(_message(100))
        job = _job(monitor)
        assert monitor.record(manual(101, sender=901)) is None
        assert inbox.outgoing_context(42) == []
        key = inbox.reserve_effect(
            **guard(job), kind="result", text_sha256="b" * 64, peer_id=42, reply_to_message_id=100
        )
        inbox.verify_effect(effect_key=key, receipt_key=key, message_id=102)
        assert monitor.record(manual(102)) is None
        before = inbox.execution_authority(**guard(job))
        assert monitor.record(manual(102)) is None
        assert inbox.execution_authority(**guard(job)) == before
        assert inbox.read_job(job["job_id"])["state"] == "running" and inbox.status()["open_events"] == 1
    finally:
        inbox.close()


def test_trusted_codex_guidance_pause_takeover_and_explicit_resume(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            monitor.record(_message(100, text="Этого клиента веду я"))
            job = _job(monitor)
            client = Client([_message(100)])
            assert job["client_control"]["state"] == "active"  # Client text has no authority.
            for action in ("check_variant", "context", "reconsider"):
                revision = inbox.client_control(42)["revision"]
                args = bridge.build_parser().parse_args(
                    [
                        "--account",
                        "work",
                        "job-client-control",
                        "--job-id",
                        job["job_id"],
                        "--action",
                        action,
                        "--expected-control-revision",
                        str(revision),
                        "--text",
                        "Дополнительные сведения",
                    ]
                )
                response = await bridge._handle_job_operation(
                    client,
                    config,
                    bridge._job_cli_request(args),
                    monitor,
                )
                assert response["client_control"]["revision"] == revision + 1
                state = await bridge._handle_job_operation(
                    client, config, {"operation": "job_read", "job_id": job["job_id"]}, monitor
                )
                assert state["job"]["trusted_guidance"][-1]["action"] == action
                with pytest.raises(InboxError, match="inbox_context_stale"):
                    await _send(client, config, monitor, job, kind="result", text="Старый ответ")
                job = inbox.refresh_job(**guard(job))
            inbox.set_client_control(42, action="pause", expected_revision=3)
            with pytest.raises(InboxError, match="inbox_client_paused"):
                inbox.execution_authority(**guard(job))
            done = inbox.result(
                **guard(job),
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[],
                reason_code="paused",
            )
            close_turn(inbox, done)
            inbox.set_client_control(42, action="takeover", expected_revision=4)
            monitor.record(_message(101, text="Новый вопрос"))
            assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            inbox.set_client_control(42, action="resume", expected_revision=5)
            assert _job(monitor)["state"] == "running"
            assert client.sent == 0
            with pytest.raises(bridge.BridgeError, match="inbox_client_target_required"):
                await bridge._handle_job_operation(
                    client, config, {"operation": "job_client_control", "action": "pause"}, monitor
                )
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_missing_transient_guidance_after_restart_blocks_new_send(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            monitor.record(_message(100))
            job = _job(monitor)
            inbox.set_client_control(42, action="context", expected_revision=0)
            job = inbox.refresh_job(**guard(job))
            with pytest.raises(bridge.BridgeError, match="inbox_guidance_unavailable"):
                await bridge._handle_operation(
                    Client([_message(100)]),
                    config,
                    {
                        "operation": "send",
                        "job_id": job["job_id"],
                        "lease": job["lease"],
                        "revision": job["revision"],
                        "kind": "result",
                        "text": "Ответ",
                        "peer": "42",
                        "reply_to_message_id": 100,
                        "mode": "dry_run",
                    },
                    inbound_monitor=monitor,
                )
            assert inbox.read_job(job["job_id"])["effects"] == []
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_proven_failed_start_waits_new_input_but_unknown_send_stays_guarded(tmp_path):
    config, monitor = _runtime(tmp_path)
    del config
    inbox = monitor.inbox
    try:
        first = monitor.record(_message(100))
        job = _job(monitor, running=False)
        mapping = inbox.prepare_peer_thread(**guard(job))
        inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="test-thread")
        job = inbox.start_intent(**guard(job), thread_id="test-thread")
        stopped = inbox.finish_execution(
            **guard(job), proof_kind="start_rejected", thread_id="test-thread", local_tasks_done=True
        )
        failed = inbox.defer_failed_request(**guard(stopped))
        assert failed["state"] == "failed_waiting_input" and inbox.execution_slot() is None
        assert monitor.record(_message(100)) == first
        assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
        monitor.record(_message(101, text="Хочу узнать цену ремонта"))
        recovered = _job(monitor)
        assert len(recovered["event_ids"]) == 2
        key = inbox.reserve_effect(
            **guard(recovered), kind="result", text_sha256="b" * 64, peer_id=42, reply_to_message_id=100
        )
        inbox.mark_effect_intent(key)
        stopped = inbox.finish_execution(
            **guard(recovered),
            proof_kind="terminal",
            thread_id=recovered["thread_id"],
            turn_id=recovered["turn_id"],
            terminal_status="failed",
            local_tasks_done=True,
        )
        with pytest.raises(InboxError, match="inbox_failure_recovery_unsafe"):
            inbox.defer_failed_request(**guard(stopped))
        assert inbox.read_job(recovered["job_id"])["state"] == "needs_review"
    finally:
        inbox.close()


def test_schema3_reopen_preserves_pending_events_unknown_send_and_pause(tmp_path):
    config, monitor = _runtime(tmp_path)
    del config
    inbox = monitor.inbox
    path = inbox.path
    try:
        event = monitor.record(_message(100))
        job = _job(monitor)
        key = inbox.reserve_effect(
            **guard(job), kind="result", text_sha256="b" * 64, peer_id=42, reply_to_message_id=100
        )
        inbox.mark_effect_intent(key)
        job = inbox.needs_review(**guard(job), reason_code="send_outcome_uncertain")
        inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
        before = inbox.read_job(job["job_id"])
        # Actual schema3 had neither of these new tables. Its identities,
        # effect intents, leases and execution slot must migrate untouched.
        inbox._db.execute("DROP TABLE outgoing_observations")
        inbox._db.execute("DROP TABLE client_controls")
        inbox._db.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
        inbox._db.commit()
    finally:
        inbox.close()
    from autostop_manager.telegram_inbox import TelegramInbox

    with TelegramInbox(path) as restored:
        assert restored.read_job(job["job_id"]) == before
        assert restored.require_open(event).message_id == 100
        assert restored.status()["execution_paused"] is True
        assert restored.status()["schema_version"] == 4
        with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
            restored.result(
                **guard(job),
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[],
                reason_code="not_actionable",
            )


def test_verified_ack_survives_failed_turn_without_closing_or_repeating_request(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        inbox = monitor.inbox
        try:
            incoming = _message(100, text="Проверьте масло коробки и редуктора")
            first = monitor.record(incoming)
            job = _job(monitor)
            client = Client([incoming])
            dry = await _send(client, config, monitor, job, kind="ack", text="Проверю оба агрегата")
            ack = await _send(
                client, config, monitor, job, kind="ack", text="Проверю оба агрегата", mode="apply", dry=dry
            )
            with pytest.raises(InboxError, match="inbox_result_receipt_required"):
                inbox.result(
                    **guard(job), outcome="answered", event_ids=job["event_ids"], receipt_keys=[ack["receipt_key"]]
                )
            stopped = inbox.finish_execution(
                **guard(job),
                proof_kind="terminal",
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                terminal_status="failed",
                local_tasks_done=True,
            )
            inbox.defer_failed_request(**guard(stopped))
            assert monitor.record(incoming) == first
            assert inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40) is None
            monitor.record(_message(102, text="Есть результат?"))
            recovered = _job(monitor)
            assert len(recovered["event_ids"]) == 2 and recovered["effects"] == []
            assert recovered["prior_acknowledgements"] == [
                {
                    "effect_key": ack["receipt_key"],
                    "job_id": job["job_id"],
                    "receipt_key": ack["receipt_key"],
                    "sent_message_id": 101,
                    "reply_to_message_id": 100,
                }
            ]
            with pytest.raises(InboxError, match="inbox_receipt_unverified"):
                inbox.result(
                    **guard(recovered),
                    outcome="answered",
                    event_ids=recovered["event_ids"],
                    receipt_keys=[ack["receipt_key"]],
                )
            assert client.sent == 1
        finally:
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner_notification", [False, True])
def test_native_send_ledger_identifies_agent_outgoing_on_exact_transport_peer(
    tmp_path, monkeypatch, owner_notification
):
    config, monitor = _runtime(tmp_path)
    monitor.self_peer_id = 900
    monitor.config = config
    inbox = monitor.inbox
    try:
        key = bridge._ensure_private_key(config.state_dir / "contract.key")
        entry = {"operation": "send_text", "peer_id": 42, "message_id": 101, "readback_verified": True}
        if owner_notification:
            monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config: 42)
            digest = hmac.new(key, b"owner\x0042", hashlib.sha256).digest()
            entry = {
                "operation": "send_owner_notification",
                "target_role": "owner",
                "message_id": 101,
                "target_fingerprint": f"{(int.from_bytes(digest[:8], 'big') or 1):016x}",
                "readback_verified": True,
            }
        bridge._save_idempotency(config.state_dir / "idempotency.json", {"original-send": entry})
        monitor.record(_message(100))
        job = _job(monitor)
        assert monitor.observe_self_message(manual(101)) == "agent"
        assert inbox.read_job(job["job_id"])["context_revision"] == inbox._peer_revision(42)
        assert monitor.observe_self_message(manual(101, peer=43)) == "human"
        # Attribution by a known message ID is independent of delivery receipt
        # availability; it still must never become a fake operation receipt.
        entry["readback_verified"] = False
        bridge._save_idempotency(config.state_dir / "idempotency.json", {"original-send": entry})
        assert monitor.observe_self_message(manual(101)) == "agent"
        assert inbox.read_job(job["job_id"])["effects"] == []
    finally:
        inbox.close()


def test_unknown_native_send_blocks_same_peer_new_effect_and_human_completion(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        monitor.config = config
        inbox = monitor.inbox
        try:
            monitor.record(_message(100))
            job = _job(monitor)
            pending = {"operation": "send_text", "peer_id": 42, "message_id": 0, "readback_verified": False}
            bridge._save_idempotency(config.state_dir / "idempotency.json", {"original-send": pending})
            assert monitor.observe_self_message(manual(101)) == "unresolved"
            assert monitor.observe_self_message(manual(102)) == "unresolved"
            client = Client([_message(100), manual(101), manual(102)])
            with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
                await bridge._handle_operation(
                    client,
                    config,
                    {
                        "operation": "send",
                        "job_id": job["job_id"],
                        "lease": job["lease"],
                        "revision": job["revision"],
                        "peer": "42",
                        "text": "Не дублировать",
                        "kind": "result",
                        "reply_to_message_id": 100,
                        "mode": "dry_run",
                    },
                    inbound_monitor=monitor,
                )
            with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
                await bridge._handle_job_operation(
                    client,
                    config,
                    {
                        "operation": "job_result",
                        **guard(job),
                        "outcome": "no_reply_needed",
                        "event_ids": job["event_ids"],
                        "receipt_keys": [],
                        "reason_code": "human_answered",
                        "human_message_ids": [102],
                    },
                    monitor,
                )
            assert bridge._native_outgoing_origin(config, 43, -1) is None
            # Model exact original send reconciliation. Reusing the same key
            # identifies 101 as agent; the remaining 102 is then human context.
            bridge._persist_delivery(
                config.state_dir / "idempotency.json", "original-send", pending, message_id=101, verified=True
            )
            monitor.reconcile_outgoing(42)
            assert inbox.outgoing_context(42) == [
                {"message_id": 102, "origin": "human"},
                {"message_id": 101, "origin": "agent"},
            ]
            assert client.sent == 0 and inbox.read_job(job["job_id"])["effects"] == []
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_native_telethon_outgoing_without_from_id_uses_authenticated_session(tmp_path, monkeypatch):
    from telethon.tl.types import Message, PeerUser

    config, monitor = _runtime(tmp_path)
    monitor.self_peer_id = 900
    try:
        monitor.record(_message(100))
        job = _job(monitor)
        outgoing = Message(id=101, peer_id=PeerUser(42), out=True, message="Ответ человека")
        assert outgoing.sender_id is None
        assert monitor.record(outgoing) is None
        assert monitor.inbox.outgoing_context(42) == [{"message_id": 101, "origin": "human"}]
        foreign = Message(id=102, peer_id=PeerUser(42), from_id=PeerUser(901), out=True, message="Не наш sender")
        assert monitor.observe_self_message(foreign) is None
        assert monitor.inbox.status()["open_events"] == 1
        job = monitor.inbox.refresh_job(**guard(job))

        async def resolve(_client, peer):
            return int(peer), {"id": int(peer), "kind": "private"}

        monkeypatch.setattr(bridge, "_resolve_peer", resolve)
        done = asyncio.run(
            bridge._handle_job_operation(
                Client([outgoing]),
                config,
                {
                    "operation": "job_result",
                    **guard(job),
                    "outcome": "no_reply_needed",
                    "event_ids": job["event_ids"],
                    "receipt_keys": [],
                    "reason_code": "human_answered",
                    "human_message_ids": [101],
                },
                monitor,
            )
        )
        assert done["job"]["state"] == "completed" and done["job"]["effects"] == []
    finally:
        monitor.inbox.close()
