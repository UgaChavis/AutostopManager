"""A delivered question can finish after pause without reopening or resending it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_telegram_bridge_durable import Client, _job, _message, _runtime, _send
from test_telegram_fresh_review_regressions import control
from test_telegram_human_control import close_turn, guard
from test_telegram_wake_durable import FakeJobs

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


@pytest.fixture(autouse=True)
def fake_state_request(monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)


async def finish_question(config, monitor, client, job, receipt_keys):
    return await bridge._handle_job_operation(
        client,
        config,
        {
            "operation": "job_result",
            **guard(job),
            "outcome": "awaiting_client",
            "event_ids": job["event_ids"],
            "receipt_keys": receipt_keys,
        },
        monitor,
    )


async def question(config, monitor, client, job, kind="clarification"):
    dry = await _send(client, config, monitor, job, kind=kind, text="Synthetic quantity question")
    return await _send(
        client, config, monitor, job, kind=kind, text="Synthetic quantity question", mode="apply", dry=dry
    )


@pytest.mark.parametrize("action,state", [("pause", "paused"), ("takeover", "human_takeover")])
@pytest.mark.parametrize("lost_guidance", [False, True])
def test_verified_question_finishes_truthfully_under_late_control(tmp_path, action, state, lost_guidance):
    async def run():
        config, monitor = _runtime(tmp_path)
        try:
            source = _message(100)
            event = monitor.record(source)
            job = _job(monitor)
            client = Client([source])
            receipt = await question(config, monitor, client, job)
            if lost_guidance:
                control(monitor, "context", "Synthetic pending check for the continuation")
                monitor.trusted_guidance.clear()
            control(monitor, action)
            completed = (await finish_question(config, monitor, client, job, [receipt["receipt_key"]]))["job"]
            assert completed["state"] == "completed" and completed["outcome"] == "awaiting_client"
            assert monitor.inbox.event_state(event) == "reply_verified"
            assert completed["effects"][0]["receipt_key"] == receipt["receipt_key"]
            assert completed["effects"][0]["verified"] and client.sent == 1
            assert completed["client_control"]["state"] == state
            assert completed["client_control"]["guidance_pending"] == int(lost_guidance)
            with pytest.raises(InboxError, match="inbox_client_" + state):
                monitor.inbox.require_client_send_allowed(42)
            close_turn(monitor.inbox, completed)
            assert monitor.inbox.execution_slot() is None
            control(monitor, "resume")
            new_event = monitor.record(_message(103, text="Synthetic answer to the delivered question"))
            next_job = monitor.inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)
            assert next_job is not None and next_job["event_ids"] == [new_event]
            assert next_job["job_id"] != job["job_id"] and client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "takeover"])
def test_actual_wake_finish_releases_delivered_question_without_review_hold(tmp_path, action):
    async def run():
        config, monitor = _runtime(tmp_path)
        try:
            source = _message(100)
            monitor.record(source)
            job = _job(monitor)
            client = Client([source])
            receipt = await question(config, monitor, client, job)
            control(monitor, action)
            await finish_question(config, monitor, client, job, [receipt["receipt_key"]])
            app = wake.AppServer(wake.WakeConfig("test-thread"))
            app.connected = True
            app.ready_event.set()
            app._turn_metadata = AsyncMock(return_value="completed")
            dispatcher = wake.DurableWakeDispatcher(app, 0, FakeJobs(monitor.inbox))
            dispatcher.current_job = job
            await dispatcher._finish_request(job, None)
            completed = monitor.inbox.read_job(job["job_id"])
            assert completed["state"] == "completed" and completed["outcome"] == "awaiting_client"
            assert completed["execution_proof"] == "completed" and dispatcher.stop_proof_verified
            assert monitor.inbox.execution_slot() is None and client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "takeover"])
def test_delivered_question_can_finish_after_bridge_restart_without_ram_receipts(tmp_path, action):
    async def run():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = Client([source])
        receipt = await question(config, monitor, client, job)
        control(monitor, action)
        path = monitor.inbox.path
        monitor.inbox.close()
        with TelegramInbox(path) as restored:
            restarted = bridge.DurableInboundMonitor(restored, enabled=True)
            completed = (await finish_question(config, restarted, client, job, [receipt["receipt_key"]]))["job"]
            assert completed["state"] == "completed" and completed["outcome"] == "awaiting_client"
            assert completed["effects"][0]["receipt_key"] == receipt["receipt_key"] and client.sent == 1

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["ack_only", "no_receipt", "wrong_receipt", "unknown_effect", "new_input_active"])
def test_pause_completion_never_substitutes_for_a_verified_current_question(tmp_path, fault):
    async def run():
        config, monitor = _runtime(tmp_path)
        try:
            source = _message(100)
            monitor.record(source)
            job = _job(monitor)
            client = Client([source])
            receipt = await question(config, monitor, client, job, "ack" if fault == "ack_only" else "clarification")
            if fault == "unknown_effect":
                key = monitor.inbox.reserve_effect(
                    **guard(job), kind="mutation", text_sha256=None, peer_id=42, reply_to_message_id=100, effect_slot=1
                )
                monitor.inbox.mark_effect_intent(key)
            if fault == "new_input_active":
                monitor.record(_message(103, text="Synthetic new condition"))
            else:
                control(monitor, "pause")
            receipts = (
                []
                if fault == "no_receipt"
                else ["wrong-synthetic-key"]
                if fault == "wrong_receipt"
                else [receipt["receipt_key"]]
            )
            expected = {
                "ack_only": "inbox_result_receipt_required",
                "no_receipt": "inbox_result_receipt_required",
                "wrong_receipt": "inbox_receipt_unverified",
                "unknown_effect": "inbox_effect_outcome_unknown",
                "new_input_active": "inbox_context_stale",
            }[fault]
            with pytest.raises((InboxError, bridge.BridgeError), match=expected):
                await finish_question(config, monitor, client, job, receipts)
            assert monitor.inbox.read_job(job["job_id"])["state"] == "running"
            assert client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(run())
