from __future__ import annotations

import asyncio

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError
from test_telegram_bridge_durable import Client, _job, _message, _runtime, _send
from test_telegram_point_fixes import HistoryClient


@pytest.fixture(autouse=True)
def synthetic_transport(monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)

    async def resolve(_client, peer):
        return int(peer), {"id": int(peer), "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)


async def read_page(client, config, monitor, job, *, peer=42, before=None, receipt=None):
    request = {
        "operation": "read",
        "peer": str(peer),
        "limit": 100,
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
    }
    if before is not None:
        request["before_message_id"] = before
    if receipt is not None:
        request["history_receipt"] = receipt
    return await bridge._handle_operation(client, config, request, inbound_monitor=monitor)


async def confirm(client, config, monitor, job, receipt):
    return await bridge._handle_operation(
        client,
        config,
        {
            "operation": "job_history_confirm",
            "job_id": job["job_id"],
            "lease": job["lease"],
            "expected_revision": job["revision"],
            "history_receipt": receipt,
        },
        inbound_monitor=monitor,
    )


def test_history_over_thousand_requires_all_pages_and_confirmation(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        rows = [_message(i, out=i % 3 == 0) for i in range(1, 1202)]
        monitor.self_peer_id = 900
        for row in rows:
            if row.out:
                row.sender_id = 900
        monitor.record(rows[-1])
        job = _job(monitor)
        client = HistoryClient(rows)
        try:
            first = await read_page(client, config, monitor, job)
            assert first["has_more"] and first["history_receipt"]
            with pytest.raises(bridge.BridgeError, match="context_history_incomplete"):
                await bridge._bind_job_send(
                    client,
                    42,
                    monitor,
                    {
                        "job_id": job["job_id"],
                        "lease": job["lease"],
                        "revision": job["revision"],
                        "kind": "result",
                        "mode": "dry_run",
                    },
                    {"id": 42, "kind": "private"},
                    "draft",
                    1201,
                )
            with pytest.raises(bridge.BridgeError, match="history_sequence_invalid"):
                await read_page(client, config, monitor, job, before=1)
            with pytest.raises(bridge.BridgeError, match="history_sequence_invalid"):
                await read_page(client, config, monitor, job, before=first["next_before_message_id"])
            with pytest.raises(bridge.BridgeError, match="context_history_incomplete"):
                await confirm(client, config, monitor, job, "forged")
            seen = list(first["messages"])
            page = first
            while page["has_more"]:
                page = await read_page(
                    client, config, monitor, job, before=page["next_before_message_id"], receipt=page["history_receipt"]
                )
                seen.extend(page["messages"])
            assert len(seen) == 1201 and {row["id"] for row in seen} == set(range(1, 1202))
            assert any(row["out"] for row in seen) and any(not row["out"] for row in seen)
            assert not monitor.context_jobs[job["job_id"]]["complete"]
            await confirm(client, config, monitor, job, page["history_receipt"])
            assert monitor.context_jobs[job["job_id"]]["complete"]
            await monitor.revalidate_context(client, 42, 42, job_id=job["job_id"])
            rows[0].message = "changed old context"
            await monitor.revalidate_context(client, 42, 42, job_id=job["job_id"])
            with pytest.raises(InboxError, match="inbox_context_stale"):
                monitor.inbox.require_current_context(job["job_id"])
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_new_human_reply_after_inbound_invalidates_history(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        incoming, outgoing = _message(100), _message(101, out=True)
        outgoing.sender_id = 900
        monitor.self_peer_id = 900
        monitor.record(incoming)
        job = _job(monitor)
        try:
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await read_page(HistoryClient([incoming, outgoing]), config, monitor, job)
            assert not monitor.context_jobs[job["job_id"]]["complete"]
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_old_worker_protocol_rejected(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        try:
            with pytest.raises(bridge.BridgeError, match="worker_protocol_unsupported"):
                await bridge._handle_operation(
                    HistoryClient([]),
                    config,
                    {
                        "operation": "job_claim",
                        "worker_protocol": 2,
                        "instruction_root": "/test/snapshot",
                        "instruction_revision": "a" * 40,
                    },
                    inbound_monitor=monitor,
                )
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_history_rejects_wrong_peer_and_restart_loses_receipt(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        row = _message(1)
        monitor.record(row)
        job = _job(monitor)
        client = HistoryClient([row])
        try:
            with pytest.raises(bridge.BridgeError, match="inbound_target_mismatch"):
                await read_page(client, config, monitor, job, peer=43)
            page = await read_page(client, config, monitor, job)
            restarted = bridge.DurableInboundMonitor(monitor.inbox, enabled=True)
            with pytest.raises(bridge.BridgeError, match="context_history_incomplete"):
                await confirm(client, config, restarted, job, page["history_receipt"])
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_parallel_jobs_preserve_each_other_context(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        rows = [_message(i, peer=40 + i) for i in range(1, 6)]
        for row in rows:
            monitor.record(row)
        jobs = []
        for row in rows:
            job = monitor.inbox.claim(instruction_root="/test/snapshot", instruction_revision="a" * 40)
            assert job and job["peer_id"] == row.chat_id
            mapping = monitor.inbox.prepare_peer_thread(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"]
            )
            monitor.inbox.bind_peer_thread(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                creation_key=mapping["creation_key"],
                thread_id=f"thread-{row.chat_id}",
            )
            job = monitor.inbox.start_intent(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                thread_id=f"thread-{row.chat_id}",
            )
            jobs.append(
                monitor.inbox.bind_turn(
                    job_id=job["job_id"],
                    lease=job["lease"],
                    expected_revision=job["revision"],
                    turn_id=f"turn-{row.chat_id}",
                )
            )
        try:
            clients = [HistoryClient([row]) for row in rows]
            pages = await asyncio.gather(
                *(
                    read_page(client, config, monitor, job, peer=row.chat_id)
                    for client, job, row in zip(clients, jobs, rows, strict=True)
                )
            )
            await asyncio.gather(
                *(
                    confirm(client, config, monitor, job, page["history_receipt"])
                    for client, job, page in zip(clients, jobs, pages, strict=True)
                )
            )
            assert all(monitor.context_jobs[job["job_id"]]["complete"] for job in jobs)
            rows[0].message = "edited"
            monitor.remember_context(rows[0])
            with pytest.raises(InboxError, match="inbox_context_stale"):
                monitor.inbox.require_current_context(jobs[0]["job_id"])
            for job in jobs[1:]:
                monitor.inbox.require_current_context(job["job_id"])
                assert monitor.context_jobs[job["job_id"]]["complete"]
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_parallel_downloads_with_same_key_do_not_overwrite_receipt(tmp_path):
    from types import SimpleNamespace

    async def run():
        config, monitor = _runtime(tmp_path)
        barrier = asyncio.Event()
        arrived = 0
        content = b"%PDF-1.7\ncontent"

        class Client:
            async def get_messages(self, entity, *, ids):
                return SimpleNamespace(
                    id=ids,
                    chat_id=entity,
                    is_private=True,
                    media=SimpleNamespace(),
                    file=SimpleNamespace(name="file.pdf", mime_type="application/pdf", size=len(content)),
                )

            async def download_media(self, message, *, file):
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    barrier.set()
                await barrier.wait()
                return content

        client = Client()
        executor = bridge.EffectExecutor()
        try:
            base = [
                {"operation": "download", "peer": str(peer), "message_id": 44, "mode": "dry_run"} for peer in (41, 42)
            ]
            dry = [await bridge._handle_operation(client, config, request) for request in base]
            requests = [
                request | {"mode": "apply", "idempotency_key": "same-key", "contract_token": result["contract_token"]}
                for request, result in zip(base, dry, strict=True)
            ]
            results = await asyncio.gather(
                *(
                    executor.submit(
                        lambda request=request: bridge._handle_operation(client, config, request),
                        timeout=5,
                        job_id=f"job-{i}",
                    )
                    for i, request in enumerate(requests)
                ),
                return_exceptions=True,
            )
            assert sum(isinstance(result, dict) for result in results) == 1
            errors = [result for result in results if isinstance(result, Exception)]
            assert len(errors) == 1 and str(errors[0]) == "idempotency_key_conflict"
            ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")
            successful = next(result for result in results if isinstance(result, dict))
            assert ledger["same-key"]["peer_id"] == successful["target"]["id"]
            assert len(list((config.socket_path.parent / "inbox").iterdir())) == 1
        finally:
            await executor.close()
            monitor.inbox.close()

    asyncio.run(run())


def test_long_revalidation_allows_progress_renewal_and_scales_budget(tmp_path):
    from datetime import UTC, datetime, timedelta

    async def run():
        config, monitor = _runtime(tmp_path)
        rows = [_message(i) for i in range(1, 302)]
        monitor.record(rows[-1])
        job = _job(monitor)

        class SlowClient(HistoryClient):
            async def get_messages(self, entity, *, ids=None, limit=None, offset_id=None):
                if isinstance(ids, list):
                    await asyncio.sleep(0.1)
                return await super().get_messages(entity, ids=ids, limit=limit, offset_id=offset_id)

        client = SlowClient(rows)
        try:
            page = await read_page(client, config, monitor, job)
            while page["has_more"]:
                page = await read_page(
                    client, config, monitor, job, before=page["next_before_message_id"], receipt=page["history_receipt"]
                )
            await confirm(client, config, monitor, job, page["history_receipt"])
            deadline = (datetime.now(UTC) + timedelta(seconds=0.2)).isoformat()
            monitor.inbox._db.execute("UPDATE jobs SET deadline_at=? WHERE job_id=?", (deadline, job["job_id"]))
            monitor.inbox._db.commit()
            request = {
                "operation": "send",
                "mode": "dry_run",
                "job_id": job["job_id"],
                "history_pages": page["history_pages"],
            }
            assert bridge._request_budget(request, monitor) > 100
            with pytest.raises(bridge.BridgeError, match="history_pages_mismatch"):
                bridge._request_budget(request | {"history_pages": 1}, monitor)
            authority = monitor.inbox.execution_authority(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"]
            )
            token = bridge._EFFECT_AUTHORITY.set(bridge.EffectAuthority(monitor.inbox, tuple(authority.items())))

            async def renew():
                await asyncio.sleep(0.1)
                monitor.inbox.progress(job_id=job["job_id"], lease=job["lease"])

            try:
                await asyncio.gather(monitor.revalidate_context(client, 42, 42, job_id=job["job_id"]), renew())
            finally:
                bridge._EFFECT_AUTHORITY.reset(token)
            assert datetime.now(UTC) > datetime.fromisoformat(deadline)
        finally:
            monitor.inbox.close()

    asyncio.run(run())


def test_old_ack_cannot_bypass_history_after_new_client_message(tmp_path):
    async def run():
        config, monitor = _runtime(tmp_path)
        first = _message(100)
        first_id = monitor.record(first)
        job = _job(monitor)
        client = Client([first])
        try:
            dry = await _send(client, config, monitor, job, kind="ack", text="received")
            ack = await _send(client, config, monitor, job, kind="ack", text="received", mode="apply", dry=dry)
            second = _message(102, text="new client question")
            client.messages[second.id] = second
            second_id = monitor.record(second)
            job = monitor.inbox.refresh_job(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                event_ids=[first_id, second_id],
            )
            request = {
                "operation": "job_result",
                "job_id": job["job_id"],
                "lease": job["lease"],
                "expected_revision": job["revision"],
                "outcome": "no_reply_needed",
                "event_ids": [first_id, second_id],
                "receipt_keys": [ack["receipt_key"]],
                "reason_code": "not_actionable",
            }
            with pytest.raises(bridge.BridgeError, match="context_history_incomplete"):
                await bridge._handle_job_operation(client, config, request, monitor)
            assert monitor.inbox.event_state(first_id) == monitor.inbox.event_state(second_id) == "open"
            page = await read_page(client, config, monitor, job)
            assert not page["has_more"]
            await confirm(client, config, monitor, job, page["history_receipt"])
            result = await bridge._handle_job_operation(client, config, request, monitor)
            assert result["job"]["state"] == "completed"
            assert monitor.inbox.event_state(first_id) == monitor.inbox.event_state(second_id) == "no_reply_needed"
            assert client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(run())
