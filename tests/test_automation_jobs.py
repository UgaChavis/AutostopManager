from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, current_thread
from uuid import uuid4

import pytest
import httpx

from autostop_manager import automation_registry as registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_jobs import (
    CRM_DIGEST_CONSUMER_ID,
    AutomationJobError,
    CrmDigestExecutor,
    HttpCrmDigestSource,
    TelegramOwnerNotifier,
    render_crm_digest,
)
from autostop_manager.automation_registry import AutomationStore, canonical_json
from autostop_manager.config import AutomationCrmConnectionConfig


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_real_http_client_preserves_existing_auth(monkeypatch, host):
    observed = []
    real_client = httpx.AsyncClient

    def respond(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "format": "crm_change_feed_readiness_v1",
                    "consumer_id": CRM_DIGEST_CONSUMER_ID,
                    "consumer_registered": False,
                    "pending_delivery": False,
                    "pending_publish": False,
                    "high_water": 0,
                    "generation": "synthetic-generation",
                },
            },
        )

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("autostop_manager.automation_jobs.httpx.AsyncClient", client)
    source = HttpCrmDigestSource(
        AutomationCrmConnectionConfig(
            configured=True, api_url=f"http://{host}:8000", bearer_token="synthetic-existing-bearer"
        )
    )
    assert source.readiness()["high_water"] == 0
    assert len(observed) == 1
    assert str(observed[0].url) == f"http://{host}:8000/api/change_feed/readiness"
    assert observed[0].headers["Authorization"] == "Bearer synthetic-existing-bearer"
    assert observed[0].headers["X-Autostop-Automation-Protocol"] == "crm_digest_v1"


def command(service, operation, payload, key, revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "digest-test", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


def claimed_digest_run(tmp_path: Path, owner: str = "worker-a"):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = command(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1"},
        "create-digest-0001",
    )["job"]
    job = command(
        service,
        "set_enabled",
        {"job_id": job["job_id"], "enabled": True},
        "enable-digest-0001",
        job["revision"],
    )["job"]
    reconciled = store.reconcile_next_job()
    assert reconciled and reconciled["applied"] is True
    job = reconciled["job"]
    command(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        "run-digest-0001",
        job["revision"],
    )
    claim = store.claim_next_run(owner=owner)
    assert claim
    return store, service, job, {**claim, "lease_owner": owner}


def digest_payload(*, ack: str = "full-window-ack", groups: int = 3, raw_events: int = 5):
    categories = ["movement", "repair_order", "finance", "inventory", "other"]
    items = [
        {
            "category": categories[index % len(categories)],
            "count": 1,
            "actions": ["card_updated"],
            "crm_path": f"/?card_id=card-{index + 1}",
        }
        for index in range(min(groups, 12))
    ]
    category_counts = dict.fromkeys(categories, 0)
    for index in range(groups):
        category_counts[categories[index % len(categories)]] += 1
    snapshot = {
        "format": "crm_change_digest_v1",
        "digest_id": "digest-" + "a" * 32,
        "generation": "generation-1",
        "consumer_id": CRM_DIGEST_CONSUMER_ID,
        "from_sequence": 11,
        "through_sequence": 15,
        "delivery_high_water": 15,
        "total_events": groups,
        "raw_event_count": raw_events,
        "category_counts": category_counts,
        "financial_totals": {
            "income_minor": 123_45,
            "expense_minor": 0,
            "refund_minor": 0,
            "cancel_minor": 0,
            "unknown_amount_events": 0,
        },
        "items": items,
        "omitted_groups": max(0, groups - len(items)),
        "created_at": "2026-09-21T12:00:00+00:00",
    }
    return {
        **snapshot,
        "ack": ack,
        "content_hash": hashlib.sha256(canonical_json(snapshot).encode()).hexdigest(),
        "replayed": False,
    }


class FakeSource:
    def __init__(self, *, events=True):
        self.events = events
        self.calls = []
        self.digest = digest_payload()
        self.acked_sequence = 10

    def register(self):
        self.calls.append(("register",))
        return {"consumer_id": CRM_DIGEST_CONSUMER_ID, "acked_sequence": self.acked_sequence}

    def read(self, *, cursor=None):
        self.calls.append(("read", cursor))
        has_events = self.events and self.acked_sequence < 15
        return {
            "consumer_id": CRM_DIGEST_CONSUMER_ID,
            "events": [{"technical": True}] if has_events else [],
            "ack": "first-page-ack" if has_events else None,
            "next_cursor": "must-not-be-followed" if has_events else None,
        }

    def summarize(self, *, ack):
        self.calls.append(("summarize", ack))
        return self.digest

    def acknowledge(self, *, ack):
        self.calls.append(("ack", ack))
        self.acked_sequence = 15
        return {"acked_sequence": 15, "delivery_complete": True}


class FakeNotifier:
    def __init__(self, *, fail_apply=False, lookup_result=None):
        self.fail_apply = fail_apply
        self.lookup_result = lookup_result
        self.previewed = []
        self.applied = []
        self.readbacks = []
        self.lookups = []

    def preview(self, text):
        self.previewed.append(text)
        return {
            "ok": True,
            "mode": "dry_run",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "contract_token": "contract-token",
        }

    def apply(self, text, *, contract_token, idempotency_key):
        self.applied.append((text, contract_token, idempotency_key))
        if self.fail_apply:
            raise AutomationJobError("automation_telegram_outcome_unknown", outcome_uncertain=True)
        return {"ok": True, "verified": True, "message_id": 42}

    def readback(self, *, message_id, text_sha256):
        self.readbacks.append((message_id, text_sha256))
        return {"ok": True, "verified": True, "message_id": message_id, "text_sha256": text_sha256}

    def lookup(self, *, idempotency_key, text_sha256):
        self.lookups.append((idempotency_key, text_sha256))
        return self.lookup_result or {
            "ok": True,
            "outcome": "not_found",
            "found": False,
            "verified": False,
        }


def test_digest_executor_sends_one_full_window_summary_and_acks_only_after_readback(tmp_path: Path):
    store, _service, _job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()
    notifier = FakeNotifier()
    executor = CrmDigestExecutor(store=store, source=source, notifier=notifier)

    result = executor(claim)

    assert result == {"ok": True, "result_code": "ok", "pages": 1, "events": 3}
    assert len(notifier.applied) == 1
    assert len(notifier.readbacks) == 1
    assert source.calls == [
        ("register",),
        ("read", None),
        ("summarize", "first-page-ack"),
        ("ack", "full-window-ack"),
    ]
    assert "приход: 123,45 ₽" in notifier.previewed[0]
    cursors = store.get_cursors(job_id=claim["job_id"])
    assert all(item["value"] is None for item in cursors.values())


def test_digest_executor_does_not_send_or_ack_when_no_changes(tmp_path: Path):
    store, _service, _job, claim = claimed_digest_run(tmp_path)
    source = FakeSource(events=False)
    notifier = FakeNotifier()

    result = CrmDigestExecutor(store=store, source=source, notifier=notifier)(claim)

    assert result["result_code"] == "no_changes"
    assert notifier.previewed == []
    assert source.calls == [("register",), ("read", None)]


def test_unknown_send_is_held_without_ack_or_blind_retry(tmp_path: Path):
    store, service, job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()
    notifier = FakeNotifier(fail_apply=True)
    executor = CrmDigestExecutor(store=store, source=source, notifier=notifier)

    first = executor(claim)

    assert first["ok"] is False
    assert first["outcome_uncertain"] is True
    assert not any(call[0] == "ack" for call in source.calls)
    assert store.get_cursors(job_id=job["job_id"])["delivery_state"]["value"] == "sending"
    assert (
        store.finish_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="worker-a",
            fencing_token=claim["fencing_token"],
            succeeded=False,
            result_code=first["result_code"],
        )
        is True
    )
    command(service, "run_now", {"job_id": job["job_id"]}, "run-digest-0002", job["revision"])
    second_claim = store.claim_next_run(owner="worker-a")
    assert second_claim

    second = executor({**second_claim, "lease_owner": "worker-a"})

    assert second["result_code"] == "automation_delivery_manual_reconciliation_required"
    assert len(notifier.applied) == 1
    assert len(notifier.lookups) == 1
    assert not any(call[0] == "ack" for call in source.calls)


def test_unknown_send_with_stored_idempotency_is_read_back_then_acked(tmp_path: Path):
    store, service, job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()
    first_notifier = FakeNotifier(fail_apply=True)
    first = CrmDigestExecutor(store=store, source=source, notifier=first_notifier)(claim)
    text_hash = store.get_cursors(job_id=job["job_id"])["notification_text_hash"]["value"]
    assert store.finish_run(
        job_id=job["job_id"],
        run_id=claim["run_id"],
        owner="worker-a",
        fencing_token=claim["fencing_token"],
        succeeded=False,
        result_code=first["result_code"],
    )
    command(service, "run_now", {"job_id": job["job_id"]}, "run-digest-0003", job["revision"])
    second_claim = store.claim_next_run(owner="worker-a")
    assert second_claim
    second_notifier = FakeNotifier(
        lookup_result={
            "ok": True,
            "outcome": "verified",
            "found": True,
            "verified": True,
            "message_id": 42,
            "text_sha256": text_hash,
        }
    )

    second = CrmDigestExecutor(store=store, source=source, notifier=second_notifier)(
        {**second_claim, "lease_owner": "worker-a"}
    )

    assert second["result_code"] == "no_changes"
    assert second_notifier.applied == []
    assert second_notifier.lookups == [("crm-digest:digest-" + "a" * 32, text_hash)]
    assert second_notifier.readbacks == [(42, text_hash)]
    assert ("ack", "full-window-ack") in source.calls
    assert all(item["value"] is None for item in store.get_cursors(job_id=job["job_id"]).values())


def test_known_pre_send_rejection_retries_digest_with_original_key_and_body(monkeypatch, tmp_path):
    store, service, job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()
    requests = []
    applies = []

    def local_request(_socket, payload, *, timeout_seconds):
        requests.append(dict(payload))
        if payload["operation"] == "owner_notification_readback":
            return {
                "ok": True,
                "verified": True,
                "message_id": payload["message_id"],
                "text_sha256": payload["expected_text_sha256"],
            }
        assert payload["operation"] == "send_owner_notification"
        if payload["mode"] == "dry_run":
            return {
                "ok": True,
                "mode": "dry_run",
                "text_sha256": hashlib.sha256(payload["text"].encode()).hexdigest(),
                "contract_token": f"contract-{len(requests)}",
            }
        applies.append(dict(payload))
        if len(applies) == 1:
            return {"ok": False, "error": "conversation_changed_since_dry_run"}
        return {"ok": True, "verified": True, "message_id": 42}

    monkeypatch.setattr("autostop_manager.automation_jobs.send_local_request", local_request)
    notifier = TelegramOwnerNotifier(tmp_path / "unused.sock")
    executor = CrmDigestExecutor(store=store, source=source, notifier=notifier)

    first = executor(claim)

    assert first == {
        "ok": False,
        "result_code": "automation_notification_preview_stale",
        "outcome_uncertain": False,
    }
    assert store.get_cursors(job_id=job["job_id"])["delivery_state"]["value"] == "prepared"
    assert not any(call[0] == "ack" for call in source.calls)
    assert store.finish_run(
        job_id=job["job_id"],
        run_id=claim["run_id"],
        owner="worker-a",
        fencing_token=claim["fencing_token"],
        succeeded=False,
        result_code=first["result_code"],
    )
    command(service, "run_now", {"job_id": job["job_id"]}, "retry-known-pre-send-0001", job["revision"])
    second_claim = store.claim_next_run(owner="worker-a")
    assert second_claim

    second = executor({**second_claim, "lease_owner": "worker-a"})

    assert second["ok"] is True
    assert len(applies) == 2
    assert applies[0]["idempotency_key"] == applies[1]["idempotency_key"]
    assert applies[0]["text"] == applies[1]["text"]
    assert applies[0]["contract_token"] != applies[1]["contract_token"]
    assert ("ack", "full-window-ack") in source.calls
    assert all(item["value"] is None for item in store.get_cursors(job_id=job["job_id"]).values())


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False, "error": "telegram_operation_failed"},
        {"ok": False, "error": "send_readback_failed"},
        {"ok": False, "error": "idempotency_key_conflict"},
        {"error": "conversation_changed_since_dry_run"},
        {"ok": False, "error": ["conversation_changed_since_dry_run"]},
    ],
)
def test_owner_notifier_keeps_unproven_apply_rejections_uncertain(monkeypatch, tmp_path, response):
    monkeypatch.setattr("autostop_manager.automation_jobs.send_local_request", lambda *_args, **_kwargs: response)
    notifier = TelegramOwnerNotifier(tmp_path / "unused.sock")

    with pytest.raises(AutomationJobError, match="automation_telegram_outcome_unknown") as error:
        notifier.apply("digest body", contract_token="token", idempotency_key="digest-review-key")

    assert error.value.outcome_uncertain is True


def test_revision_change_during_preview_aborts_before_apply_or_ack(tmp_path: Path):
    store, service, job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()

    class RevisionChangingNotifier(FakeNotifier):
        def preview(self, text):
            result = super().preview(text)
            command(
                service,
                "set_schedule",
                {"job_id": job["job_id"], "schedule": {"every_minutes": 30}},
                "schedule-during-preview-0001",
                job["revision"],
            )
            return result

    notifier = RevisionChangingNotifier()

    result = CrmDigestExecutor(store=store, source=source, notifier=notifier)(claim)

    assert result["result_code"] == "automation_claim_superseded"
    assert notifier.applied == []
    assert not any(call[0] == "ack" for call in source.calls)


@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.parametrize("transition", ["revision", "release_hold"])
def test_digest_ack_requires_current_claim_after_verified_readback(tmp_path: Path, monkeypatch, recovering, transition):
    store, service, job, claim = claimed_digest_run(tmp_path)
    job_id = job["job_id"]
    revision = job["revision"]
    source = FakeSource()
    original_notifier = FakeNotifier()

    def next_claim(key, owner):
        command(service, "run_now", {"job_id": job_id}, key, revision)
        claimed = store.claim_next_run(owner=owner)
        assert claimed
        return {**claimed, "lease_owner": owner}

    if recovering:
        monkeypatch.setattr(original_notifier, "readback", lambda **_kwargs: {"verified": False})
        interrupted = CrmDigestExecutor(store=store, source=source, notifier=original_notifier)(claim)
        assert interrupted["result_code"] == "automation_delivery_readback_failed"
        assert store.finish_run(
            job_id=job_id,
            run_id=claim["run_id"],
            owner=claim["lease_owner"],
            fencing_token=claim["fencing_token"],
            succeeded=False,
            result_code=interrupted["result_code"],
        )
        claim = next_claim("run-after-interrupted-readback", "worker-b")

    def set_hold(enabled, expected_revision):
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = store.set_global_hold(
                connection,
                enabled=enabled,
                reason="release" if enabled else None,
                actor_hash=hashlib.sha256(b"synthetic-release-operator").hexdigest(),
                attempt_hash="d" * 64,
                expected_revision=expected_revision,
            )
        assert result["global_hold"]["enabled"] is enabled

    notifier = FakeNotifier() if recovering else original_notifier
    readback = notifier.readback

    def transition_during_readback(**kwargs):
        nonlocal revision
        receipt = readback(**kwargs)
        if transition == "revision":
            changed = command(
                service,
                "set_schedule",
                {"job_id": job_id, "schedule": {"every_minutes": 30}},
                "schedule-during-readback-0001",
                revision,
            )
            assert changed["job"]["revision"] == revision + 1
            revision = changed["job"]["revision"]
        else:
            set_hold(True, 0)
        return receipt

    monkeypatch.setattr(notifier, "readback", transition_during_readback)
    invalidated = CrmDigestExecutor(store=store, source=source, notifier=notifier)(claim)
    assert invalidated["result_code"] == "automation_claim_superseded"
    assert not any(call[0] == "ack" for call in source.calls)
    assert len(original_notifier.applied) == 1
    assert store.get_cursors(job_id=job_id)["delivery_state"]["value"] == "delivered_unacked"
    assert store.cancel_run_claim(
        job_id=job_id, run_id=claim["run_id"], owner=claim["lease_owner"], fencing_token=claim["fencing_token"]
    )
    if transition == "release_hold":
        assert store.claim_next_run(owner="worker-c") is None
        set_hold(False, 1)

    fresh_notifier = FakeNotifier()
    fresh_claim = next_claim("run-after-control-transition", "worker-c")
    result = CrmDigestExecutor(store=store, source=source, notifier=fresh_notifier)(fresh_claim)
    assert result["result_code"] == "no_changes"
    assert fresh_notifier.readbacks == notifier.readbacks
    assert fresh_notifier.applied == []
    assert [call for call in source.calls if call[0] == "ack"] == [("ack", "full-window-ack")]
    assert all(cursor["value"] is None for cursor in store.get_cursors(job_id=job_id).values())


@pytest.mark.parametrize("invalidated_by", ["lease", "revision", "release_hold"])
def test_digest_sending_cursor_rechecks_claim_after_writer_wait(tmp_path: Path, monkeypatch, invalidated_by):
    clock = datetime(2030, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(registry, "utc_now", lambda: clock)
    store, service, job, claim = claimed_digest_run(tmp_path)
    job_id = job["job_id"]
    revision = job["revision"]
    deadline = registry.parse_time(claim["lease_until"])
    assert deadline is not None
    source = FakeSource()
    old_notifier = FakeNotifier()
    executor = CrmDigestExecutor(store=store, source=source, notifier=old_notifier)
    sending_cursor = Event()
    enter_write = Event()
    begin_attempted = Event()
    original_update = AutomationStore.update_cursor
    original_connect = AutomationStore.connect

    def observed_update(self, **values):
        if self is store and values["cursor_name"] == "delivery_state" and values["cursor_value"] == "sending":
            sending_cursor.set()
            assert enter_write.wait(timeout=3)
        return original_update(self, **values)

    @contextmanager
    def observed_connect(self):
        with original_connect(self) as connection:
            if self is store and current_thread().name.startswith("synthetic-digest") and sending_cursor.is_set():
                connection.set_trace_callback(lambda sql: begin_attempted.set() if sql == "BEGIN IMMEDIATE" else None)
            yield connection

    def set_hold(connection, enabled, expected_revision):
        result = store.set_global_hold(
            connection,
            enabled=enabled,
            reason="release" if enabled else None,
            actor_hash=hashlib.sha256(b"synthetic-release-operator").hexdigest(),
            attempt_hash="d" * 64,
            expected_revision=expected_revision,
        )
        assert result["global_hold"]["enabled"] is enabled

    monkeypatch.setattr(AutomationStore, "update_cursor", observed_update)
    monkeypatch.setattr(AutomationStore, "connect", observed_connect)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthetic-digest") as workers:
        waiting_run = workers.submit(executor, claim)
        try:
            assert sending_cursor.wait(timeout=3)
            with original_connect(store) as writer:
                writer.execute("BEGIN IMMEDIATE")
                enter_write.set()
                assert begin_attempted.wait(timeout=3)
                with pytest.raises(TimeoutError):
                    waiting_run.result(timeout=0.05)
                if invalidated_by == "lease":
                    clock = deadline
                elif invalidated_by == "revision":
                    changed = store.set_schedule(
                        writer, job_id=job_id, schedule_payload={"every_minutes": 30}, expected_revision=revision
                    )
                    assert changed["job"]["revision"] == revision + 1
                    revision = changed["job"]["revision"]
                else:
                    set_hold(writer, True, 0)
        finally:
            enter_write.set()
        result = waiting_run.result(timeout=3)
    expected_error = "automation_fencing_conflict" if invalidated_by == "lease" else "automation_claim_superseded"
    assert result["result_code"] == expected_error
    assert old_notifier.applied == []
    assert not any(call[0] == "ack" for call in source.calls)
    cursors = store.get_cursors(job_id=job_id)
    assert cursors["delivery_state"]["value"] == "prepared"
    assert cursors["delivery_state"]["revision"] == 1

    if invalidated_by != "lease":
        assert store.cancel_run_claim(
            job_id=job_id, run_id=claim["run_id"], owner=claim["lease_owner"], fencing_token=claim["fencing_token"]
        )
    if invalidated_by == "release_hold":
        assert store.claim_next_run(owner="worker-b") is None
        with original_connect(store) as writer:
            writer.execute("BEGIN IMMEDIATE")
            set_hold(writer, False, 1)

    command(service, "run_now", {"job_id": job_id}, "run-after-rejected-cursor-write", revision)
    new_claim = store.claim_next_run(owner="worker-b")
    assert new_claim and new_claim["fencing_token"] > claim["fencing_token"]
    new_notifier = FakeNotifier()
    new_executor = CrmDigestExecutor(store=store, source=source, notifier=new_notifier)
    resumed = new_executor({**new_claim, "lease_owner": "worker-b"})
    assert resumed["ok"] is True
    assert [attempt[2] for attempt in new_notifier.applied] == [f"crm-digest:{cursors['pending_digest_id']['value']}"]
    assert new_notifier.lookups == []
    assert [call for call in source.calls if call[0] == "ack"] == [("ack", "full-window-ack")]


@pytest.mark.parametrize(
    ("operation", "previous"),
    [("register", None), ("read", "register"), ("summarize", "read")],
)
@pytest.mark.parametrize("invalidated_by", ["revision", "release_hold"])
def test_digest_does_not_dispatch_change_feed_writes_after_supersession(
    tmp_path: Path, monkeypatch, operation, previous, invalidated_by
):
    store, service, job, claim = claimed_digest_run(tmp_path)
    source = FakeSource()
    notifier = FakeNotifier()

    def invalidate():
        if invalidated_by == "revision":
            changed = command(
                service,
                "set_schedule",
                {"job_id": job["job_id"], "schedule": {"every_minutes": 30}},
                "schedule-before-feed-write-0001",
                job["revision"],
            )
            assert changed["job"]["revision"] == job["revision"] + 1
        else:
            with store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                held = store.set_global_hold(
                    connection,
                    enabled=True,
                    reason="release",
                    actor_hash="a" * 64,
                    attempt_hash="d" * 64,
                    expected_revision=0,
                )
            assert held["global_hold"]["enabled"] is True

    if previous is None:
        invalidate()
    else:
        previous_call = getattr(source, previous)

        def invalidate_before_reply(**kwargs):
            result = previous_call(**kwargs)
            invalidate()
            return result

        monkeypatch.setattr(source, previous, invalidate_before_reply)
    outcome = CrmDigestExecutor(store=store, source=source, notifier=notifier)(claim)

    assert outcome["result_code"] == "automation_claim_superseded"
    assert operation not in {call[0] for call in source.calls}
    assert notifier.previewed == notifier.applied == []
    assert store.get_cursors(job_id=job["job_id"]) == {}


def test_renderer_uses_group_count_not_raw_event_count():
    rendered = render_crm_digest(digest_payload(groups=3, raw_events=20))

    assert rendered.startswith("CRM: 3 изм.")
    assert "20 изм." not in rendered


def test_renderer_includes_at_most_three_allowlisted_crm_links():
    rendered = render_crm_digest(digest_payload(groups=4, raw_events=8))

    assert rendered.count("https://crm.autostopcrm.ru/?card_id=") == 3
    assert "card-1" in rendered
    assert "card-4" not in rendered


def test_digest_rejects_non_allowlisted_or_noncanonical_crm_url():
    payload = digest_payload()
    payload["items"][0]["crm_path"] = "https://evil.example/?card_id=card-1"
    snapshot = {
        key: payload.get(key)
        for key in (
            "format",
            "digest_id",
            "generation",
            "consumer_id",
            "from_sequence",
            "through_sequence",
            "delivery_high_water",
            "total_events",
            "raw_event_count",
            "category_counts",
            "financial_totals",
            "items",
            "omitted_groups",
            "created_at",
        )
    }
    payload["content_hash"] = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()

    with pytest.raises(AutomationJobError, match="automation_crm_digest_invalid"):
        render_crm_digest(payload)


def test_owner_notifier_never_supplies_a_peer(monkeypatch, tmp_path: Path):
    calls = []

    def local_request(_socket, payload, *, timeout_seconds):
        calls.append((dict(payload), timeout_seconds))
        if payload["operation"] == "status":
            return {
                "ok": True,
                "transport_ready": True,
                "owner_notification_configured": True,
                "account": {"id": 123, "username": "must-not-leak"},
            }
        if payload["mode"] == "dry_run":
            return {
                "ok": True,
                "mode": "dry_run",
                "text_sha256": hashlib.sha256(payload["text"].encode()).hexdigest(),
                "contract_token": "token",
            }
        return {"ok": True, "verified": True, "message_id": 1}

    monkeypatch.setattr("autostop_manager.automation_jobs.send_local_request", local_request)
    notifier = TelegramOwnerNotifier(tmp_path / "bridge.sock")

    assert notifier.status() == {
        "transport_ready": True,
        "owner_notification_configured": True,
    }
    preview = notifier.preview("technical digest")
    notifier.apply(
        "technical digest",
        contract_token=preview["contract_token"],
        idempotency_key="crm-digest:digest-" + "a" * 32,
    )

    assert all("peer" not in payload for payload, _timeout in calls)
    assert calls[0] == ({"operation": "status"}, 2.0)


def test_crm_source_uses_reserved_protocol_header_and_existing_mcp_bearer(monkeypatch):
    observed = {}

    class Response:
        status_code = 200

        async def aclose(self):
            pass

        async def aiter_bytes(self):
            yield json.dumps(
                {
                    "ok": True,
                    "data": {
                        "format": "crm_change_feed_registration_v1",
                        "consumer_id": CRM_DIGEST_CONSUMER_ID,
                        "acked_sequence": 0,
                    },
                }
            ).encode()

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def build_request(self, method, path, *, json):
            observed["request"] = (method, path, dict(json))

        async def send(self, _request, *, stream, follow_redirects):
            assert stream is True and follow_redirects is False
            return Response()

    monkeypatch.setattr("autostop_manager.automation_jobs.httpx.AsyncClient", Client)
    source = HttpCrmDigestSource(
        AutomationCrmConnectionConfig(
            configured=True,
            api_url="http://127.0.0.1:8000",
            bearer_token="existing-mcp-token",
        )
    )

    result = source.register()

    assert result["consumer_id"] == CRM_DIGEST_CONSUMER_ID
    assert observed["client"]["headers"] == {
        "Authorization": "Bearer existing-mcp-token",
        "X-Autostop-Automation-Protocol": "crm_digest_v1",
    }
    assert observed["client"]["follow_redirects"] is False
    assert observed["client"]["trust_env"] is False
    assert observed["request"] == (
        "POST",
        "/api/change_feed/register",
        {"consumer_id": CRM_DIGEST_CONSUMER_ID, "start_at": "latest"},
    )


def test_crm_source_readiness_uses_non_mutating_probe_and_strict_contract(monkeypatch):
    observed = {}

    class Response:
        status_code = 200

        async def aclose(self):
            pass

        async def aiter_bytes(self):
            yield json.dumps(
                {
                    "ok": True,
                    "data": {
                        "format": "crm_change_feed_readiness_v1",
                        "consumer_id": CRM_DIGEST_CONSUMER_ID,
                        "consumer_registered": False,
                        "pending_delivery": False,
                        "pending_publish": False,
                        "generation": "generation-1",
                        "high_water": 41,
                    },
                }
            ).encode()

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def build_request(self, method, path, *, json):
            observed["request"] = (method, path, dict(json))

        async def send(self, _request, *, stream, follow_redirects):
            assert stream is True and follow_redirects is False
            return Response()

    monkeypatch.setattr("autostop_manager.automation_jobs.httpx.AsyncClient", Client)
    source = HttpCrmDigestSource(
        AutomationCrmConnectionConfig(
            configured=True,
            api_url="http://127.0.0.1:8000",
            bearer_token="existing-mcp-token",
        ),
        timeout_seconds=2,
    )

    assert source.readiness() == {
        "format": "crm_change_feed_readiness_v1",
        "consumer_id": CRM_DIGEST_CONSUMER_ID,
        "consumer_registered": False,
        "pending_delivery": False,
        "pending_publish": False,
        "generation": "generation-1",
        "high_water": 41,
    }
    assert 0 < observed["client"]["timeout"] <= 2
    assert observed["request"] == ("POST", "/api/change_feed/readiness", {})
