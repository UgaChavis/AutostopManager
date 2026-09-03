from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.store_integration import StoreIntegration, _merge_compact_refs


def _digest_page(
    *,
    items: list[dict] | None = None,
    next_cursor: str,
    has_more: bool,
    replay_cursor: str | None = None,
    snapshot_at: str = "2026-07-16T10:00:00+07:00",
    warnings: list[str] | None = None,
) -> dict:
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {"snapshot_at": snapshot_at},
        "items": items or [],
        "changes": [],
        "page": {
            "has_more": has_more,
            "next_cursor": next_cursor,
            "replay_cursor": replay_cursor or f"replay:{next_cursor}",
            "limit": 2,
        },
        "warnings": warnings or [],
        "meta": {"snapshot_at": snapshot_at},
    }


class _DigestClient:
    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.calls = []

    def digest(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return deepcopy(self.pages.pop(0))


class _BootstrapSnapshotClient:
    def __init__(self, result: dict):
        self.result = deepcopy(result)
        self.calls = 0

    def bootstrap_snapshot(self):
        self.calls += 1
        return deepcopy(self.result)


class _ActionClient:
    def __init__(self, *, before: dict, after: dict | None = None, action_result: dict | None = None):
        self.before = deepcopy(before)
        self.after = deepcopy(after if after is not None else before)
        self.action_result = action_result or {
            "ok": True,
            "format": "store_agent_v1",
            "status": "completed",
            "summary": {},
            "items": [],
            "changes": [],
            "page": {},
            "warnings": [],
            "meta": {},
        }
        self.context_calls = 0
        self.action_calls = []

    def entity_context(self, **_kwargs):
        self.context_calls += 1
        item = self.before if self.context_calls == 1 else self.after
        return {
            "ok": True,
            "format": "store_agent_v1",
            "status": "completed",
            "summary": {},
            "items": [deepcopy(item)],
            "changes": [],
            "page": {},
            "warnings": [],
            "meta": {},
        }

    def management_action(self, **kwargs):
        self.action_calls.append(deepcopy(kwargs))
        return deepcopy(self.action_result)


def _seed_checkpoint(store: ManagerMemoryStore, *, stream: str = "store_digest", cursor: str = "cursor-0") -> None:
    result = store.commit_store_checkpoint(
        stream=stream,
        cursor=cursor,
        last_success_at="2026-07-15T10:00:00+07:00",
        compact_refs=[],
        expected_state_version=0,
    )
    assert result["ok"] is True


def test_bootstrap_snapshot_is_stateless_and_does_not_touch_digest_checkpoints(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store, stream="store_digest", cursor="owner-digest")
    _seed_checkpoint(store, stream="store_bootstrap", cursor="legacy-bootstrap")
    expected = {
        "ok": True,
        "format": "store_agent_v1",
        "status": "ok",
        "summary": {"store_api_ready": True, "contract_version": "store_agent_v1"},
        "items": [],
        "changes": [],
        "page": {"has_more": False, "next_cursor": None},
        "warnings": [],
        "meta": {},
    }
    client = _BootstrapSnapshotClient(expected)

    result = StoreIntegration(client=client, store=store).runtime_status(
        live=True,
        bootstrap_snapshot=True,
    )

    assert result == expected
    assert client.calls == 1
    assert store.get_store_checkpoint("store_digest")["cursor"] == "owner-digest"
    assert store.get_store_checkpoint("store_bootstrap")["cursor"] == "legacy-bootstrap"


def test_first_read_creates_baseline_without_returning_historical_items(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    client = _DigestClient(
        [
            _digest_page(
                items=[{"entity": "store_order", "id": "old-order", "updated_at": "old"}],
                next_cursor="baseline-high-water",
                has_more=False,
                warnings=["store_snapshot_compact"],
            )
        ]
    )

    result = StoreIntegration(client=client, store=store).digest(limit=25)
    checkpoint = store.get_store_checkpoint()

    assert result["status"] == "baseline"
    assert result["items"] == []
    assert result["summary"]["baseline_created"] is True
    assert result["warnings"] == ["store_snapshot_compact"]
    assert result["meta"]["checkpoint_advanced"] is True
    assert checkpoint["cursor"] == "baseline-high-water"
    assert checkpoint["last_attempt_status"] == "success"
    assert client.calls == [{"baseline": True, "since": None, "cursor": None, "limit": 25}]


def test_digest_exposes_only_manager_cursor_and_strips_raw_store_cursors(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    page = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        next_cursor="raw-store-next",
        replay_cursor="raw-store-replay",
        has_more=True,
    )
    page["summary"].update(
        {
            "checkpoint_cursor": "raw-store-checkpoint",
            "next_cursor": "raw-summary-next",
            "replay_cursor": "raw-summary-replay",
            "cursor": "raw-summary-cursor",
        }
    )

    result = StoreIntegration(client=_DigestClient([page]), store=store).digest(limit=1)

    assert not {"checkpoint_cursor", "next_cursor", "replay_cursor", "cursor"} & result["summary"].keys()
    assert result["page"]["next_cursor"] not in {
        "raw-store-next",
        "raw-store-replay",
        "raw-store-checkpoint",
    }


def test_incremental_pages_above_limit_are_returned_without_advancing_until_final_page(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    first_items = [
        {"entity": "store_order", "id": "order-1", "updated_at": "v1"},
        {"entity": "store_order", "id": "order-2", "updated_at": "v2"},
    ]
    final_items = [{"entity": "store_quote_request", "id": "quote-3", "updated_at": "v3"}]
    client = _DigestClient(
        [
            _digest_page(items=first_items, next_cursor="page-2", has_more=True),
            _digest_page(items=final_items, next_cursor="cursor-final", has_more=False),
        ]
    )
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(limit=2)
    pending = store.get_store_checkpoint()

    assert first["items"] == first_items
    assert first["page"]["limit"] == 2
    assert first["page"]["returned"] == 2
    assert first["page"]["has_more"] is True
    assert first["page"]["source_has_more"] is True
    assert first["page"]["ack_required"] is True
    assert first["page"]["next_cursor"] != "page-2"
    assert first["meta"]["checkpoint_advanced"] is False
    assert pending["cursor"] == "cursor-0"
    assert pending["pending_cursor"] == "page-2"
    assert {ref["id"] for ref in pending["pending_refs"]} == {"order-1", "order-2"}

    final = integration.digest(cursor=first["page"]["next_cursor"], limit=2)
    before_final_ack = store.get_store_checkpoint()

    assert final["items"] == final_items
    assert final["page"]["has_more"] is True
    assert final["page"]["source_has_more"] is False
    assert final["page"]["ack_required"] is True
    assert final["meta"]["checkpoint_advanced"] is False
    assert before_final_ack["cursor"] == "cursor-0"
    assert before_final_ack["traversal_cursor"] == "page-2"

    ack = integration.digest(cursor=final["page"]["next_cursor"], limit=2)
    committed = store.get_store_checkpoint()

    assert ack["items"] == []
    assert ack["page"]["has_more"] is False
    assert ack["meta"]["delivery_acknowledged"] is True
    assert committed["cursor"] == "cursor-final"
    assert committed["pending_cursor"] is None
    assert {ref["id"] for ref in committed["compact_refs"]} == {"order-1", "order-2", "quote-3"}
    assert client.calls[0]["cursor"] == "cursor-0"
    assert client.calls[1]["cursor"] == "page-2"


def test_unacknowledged_page_is_replayed_from_fixed_window_and_raw_cursor_is_rejected(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    client = _DigestClient(
        [
            _digest_page(
                items=[{"entity": "store_order", "id": "first"}],
                next_cursor="old-resume",
                has_more=True,
                replay_cursor="fixed-window-start",
            ),
            _digest_page(
                items=[{"entity": "store_order", "id": "first"}],
                next_cursor="old-resume",
                has_more=True,
                replay_cursor="fixed-window-start",
            ),
        ]
    )
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(limit=1)
    replay = integration.digest(limit=1)
    checkpoint = store.get_store_checkpoint()
    stale = integration.digest(cursor="old-resume", limit=1)

    assert client.calls[0]["cursor"] == "cursor-0"
    assert client.calls[1]["cursor"] == "fixed-window-start"
    assert replay["items"] == first["items"]
    assert replay["page"]["next_cursor"] == first["page"]["next_cursor"]
    assert replay["meta"]["replayed_delivery"] is True
    assert checkpoint["cursor"] == "cursor-0"
    assert checkpoint["pending_cursor"] == "old-resume"
    assert [ref["id"] for ref in checkpoint["pending_refs"]] == ["first"]
    assert stale["status"] == "conflict"
    assert stale["summary"]["error_code"] == "store_digest_ack_stale_or_foreign"


def test_intermediate_ack_response_loss_replays_current_unacknowledged_next_page(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    page_one = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        next_cursor="page-2",
        replay_cursor="window-start",
        has_more=True,
    )
    page_two = _digest_page(
        items=[{"entity": "store_order", "id": "order-2", "updated_at": "v2"}],
        next_cursor="window-final",
        replay_cursor="page-2",
        has_more=False,
    )
    client = _DigestClient([page_one, page_two, page_two])
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(limit=1)
    second = integration.digest(cursor=first["page"]["next_cursor"], limit=1)
    retry = integration.digest(cursor=first["page"]["next_cursor"], limit=1)
    checkpoint = store.get_store_checkpoint()

    assert second["items"] == retry["items"]
    assert second["page"]["next_cursor"] == retry["page"]["next_cursor"]
    assert retry["meta"]["replayed_delivery"] is True
    assert client.calls == [
        {"baseline": False, "since": None, "cursor": "cursor-0", "limit": 1},
        {"baseline": False, "since": None, "cursor": "page-2", "limit": 1},
        {"baseline": False, "since": None, "cursor": "page-2", "limit": 1},
    ]
    assert checkpoint["cursor"] == "cursor-0"
    assert checkpoint["traversal_cursor"] == "page-2"
    assert checkpoint["pending_cursor"] == "window-final"


def test_final_ack_is_idempotent_after_commit_response_loss(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    client = _DigestClient(
        [
            _digest_page(
                items=[{"entity": "store_order", "id": "order-final", "updated_at": "v1"}],
                next_cursor="cursor-final",
                replay_cursor="window-start",
                has_more=False,
            ),
            _digest_page(
                items=[],
                next_cursor="cursor-next-cycle",
                replay_cursor="cursor-final",
                has_more=False,
            ),
        ]
    )
    integration = StoreIntegration(client=client, store=store)

    page = integration.digest(limit=1)
    first_ack = integration.digest(cursor=page["page"]["next_cursor"], limit=1)
    retry_ack = integration.digest(cursor=page["page"]["next_cursor"], limit=1)
    next_cycle = integration.digest(limit=1)
    checkpoint = store.get_store_checkpoint()

    assert first_ack == retry_ack
    assert first_ack["items"] == []
    assert first_ack["meta"]["delivery_acknowledged"] is True
    assert first_ack["page"]["next_cursor"] is None
    assert next_cycle["items"] == []
    assert next_cycle["page"]["next_cursor"] is None
    assert client.calls[1]["cursor"] == "cursor-final"
    assert checkpoint["cursor"] == "cursor-next-cycle"
    assert checkpoint["last_ack_was_final"] is False
    assert len(client.calls) == 2


def test_delivery_ack_is_bound_to_stream_but_not_rotated_by_volatile_replay_snapshot(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    first_page = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        next_cursor="page-2",
        replay_cursor="window-start",
        has_more=True,
        snapshot_at="2026-07-16T10:00:00+07:00",
    )
    replayed_page = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        next_cursor="page-2",
        replay_cursor="window-start",
        has_more=True,
        snapshot_at="2026-07-16T10:00:01+07:00",
    )
    client = _DigestClient([first_page, replayed_page])
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(limit=1)
    replay = integration.digest(limit=1)
    foreign_stream = integration.digest(
        stream="store_bootstrap",
        cursor=replay["page"]["next_cursor"],
        limit=1,
    )

    assert first["page"]["next_cursor"] == replay["page"]["next_cursor"]
    assert foreign_stream["status"] == "conflict"
    assert foreign_stream["summary"]["error_code"] == "store_digest_ack_stale_or_foreign"


def test_nonempty_page_without_fixed_replay_cursor_fails_closed(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    page = _digest_page(
        items=[{"entity": "store_order", "id": "order-1"}],
        next_cursor="page-2",
        has_more=True,
    )
    page["page"]["replay_cursor"] = None

    result = StoreIntegration(client=_DigestClient([page]), store=store).digest(limit=1)
    checkpoint = store.get_store_checkpoint()

    assert result["ok"] is False
    assert result["summary"]["error_code"] == "store_digest_missing_replay_cursor"
    assert checkpoint["cursor"] == "cursor-0"
    assert checkpoint["pending_cursor"] is None


def test_replay_reuses_original_limit_token_and_refs_after_current_entity_version_changes(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    original = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        next_cursor="page-2",
        replay_cursor="fixed-window-start",
        has_more=True,
    )
    current_projection = _digest_page(
        items=[{"entity": "store_order", "id": "order-1", "updated_at": "v2"}],
        next_cursor="page-2",
        replay_cursor="fixed-window-start",
        has_more=True,
        snapshot_at="2026-07-16T10:01:00+07:00",
    )
    client = _DigestClient([original, current_projection])
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(limit=1)
    before = store.get_store_checkpoint()
    replay = integration.digest(limit=100)
    after = store.get_store_checkpoint()

    assert client.calls[1]["limit"] == 1
    assert replay["items"][0]["updated_at"] == "v2"
    assert replay["page"]["next_cursor"] == first["page"]["next_cursor"]
    assert before["state_version"] == after["state_version"]
    assert before["pending_refs"] == after["pending_refs"]
    assert after["pending_refs"][0]["updated_at"] == "v1"


def test_replay_page_membership_mismatch_fails_without_replacing_pending_delivery(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    original = _digest_page(
        items=[{"entity": "store_order", "id": "order-1"}],
        next_cursor="page-2",
        replay_cursor="fixed-window-start",
        has_more=True,
    )
    mismatched = _digest_page(
        items=[{"entity": "store_order", "id": "order-2"}],
        next_cursor="page-2",
        replay_cursor="fixed-window-start",
        has_more=True,
    )
    integration = StoreIntegration(client=_DigestClient([original, mismatched]), store=store)

    first = integration.digest(limit=1)
    failed = integration.digest(limit=99)
    checkpoint = store.get_store_checkpoint()

    assert failed["status"] == "conflict"
    assert failed["summary"]["error_code"] == "store_digest_replay_mismatch"
    assert checkpoint["pending_cursor"] == "page-2"
    assert checkpoint["pending_page_refs"] == [{"entity": "store_order", "id": "order-1"}]
    assert checkpoint["pending_delivery_token"] == first["page"]["ack_token"]


def test_intermediate_ack_compare_and_swap_allows_only_one_traversal_advance(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = ManagerMemoryStore(db_path)
    _seed_checkpoint(store)
    page = StoreIntegration(
        client=_DigestClient(
            [
                _digest_page(
                    items=[{"entity": "store_order", "id": "order-1"}],
                    next_cursor="page-2",
                    replay_cursor="fixed-window-start",
                    has_more=True,
                )
            ]
        ),
        store=store,
    ).digest(limit=1)
    checkpoint = store.get_store_checkpoint()
    competing_store = ManagerMemoryStore(db_path)

    first = store.acknowledge_store_checkpoint_page(
        stream="store_digest",
        cursor=checkpoint["pending_cursor"],
        delivery_token=page["page"]["ack_token"],
        expected_state_version=checkpoint["state_version"],
    )
    stale = competing_store.acknowledge_store_checkpoint_page(
        stream="store_digest",
        cursor=checkpoint["pending_cursor"],
        delivery_token=page["page"]["ack_token"],
        expected_state_version=checkpoint["state_version"],
    )
    current = store.get_store_checkpoint()

    assert first["ok"] is True
    assert stale["error"] == "store_checkpoint_state_conflict"
    assert current["cursor"] == "cursor-0"
    assert current["traversal_cursor"] == "page-2"


def test_compact_refs_replace_older_versions_per_entity_and_keep_latest_bounded():
    merged = _merge_compact_refs(
        [{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
        [{"entity": "store_order", "id": "order-1", "updated_at": "v2"}],
        [{"entity": "store_order", "id": f"order-{index}", "updated_at": f"v{index}"} for index in range(2, 520)],
    )

    assert len(merged) <= 500
    assert sum(ref["id"] == "order-1" for ref in merged) <= 1
    assert merged[-1]["id"] == "order-519"


def test_failed_resume_preserves_committed_and_pending_cursors(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store)
    client = _DigestClient(
        [
            _digest_page(
                items=[{"entity": "store_order", "id": "order-1"}],
                next_cursor="resume-2",
                has_more=True,
            ),
            {
                "ok": False,
                "format": "store_agent_v1",
                "status": "degraded",
                "summary": {"error_code": "store_timeout_or_network_error"},
                "items": [],
                "page": {},
                "warnings": ["store_timeout_or_network_error"],
                "meta": {},
            },
        ]
    )
    integration = StoreIntegration(client=client, store=store)
    first = integration.digest(limit=1)

    failed = integration.digest(cursor=first["page"]["next_cursor"], limit=1)
    checkpoint = store.get_store_checkpoint()

    assert failed["ok"] is False
    assert checkpoint["cursor"] == "cursor-0"
    assert checkpoint["pending_cursor"] is None
    assert checkpoint["traversal_cursor"] == "resume-2"
    assert checkpoint["last_attempt_status"] == "degraded"


def test_bootstrap_stream_does_not_consume_primary_digest_cursor(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store, stream="store_digest", cursor="primary-cursor")
    client = _DigestClient([_digest_page(next_cursor="bootstrap-cursor", has_more=False)])

    result = StoreIntegration(client=client, store=store).digest(stream="store_bootstrap")

    assert result["meta"]["stream"] == "store_bootstrap"
    assert store.get_store_checkpoint("store_bootstrap")["cursor"] == "bootstrap-cursor"
    assert store.get_store_checkpoint("store_digest")["cursor"] == "primary-cursor"


def test_bootstrap_stream_auto_resumes_pending_page_before_committing_cursor(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    client = _DigestClient(
        [
            _digest_page(
                items=[{"entity": "store_order", "id": "order-1", "updated_at": "v1"}],
                next_cursor="bootstrap-page-2",
                has_more=True,
            ),
            _digest_page(
                items=[{"entity": "store_quote_request", "id": "quote-2", "updated_at": "v2"}],
                next_cursor="bootstrap-final",
                has_more=False,
            ),
        ]
    )
    integration = StoreIntegration(client=client, store=store)

    first = integration.digest(stream="store_bootstrap", limit=1)
    pending = store.get_store_checkpoint("store_bootstrap")
    final = integration.digest(
        stream="store_bootstrap",
        cursor=first["page"]["next_cursor"],
        limit=1,
    )
    before_ack = store.get_store_checkpoint("store_bootstrap")
    ack = integration.digest(
        stream="store_bootstrap",
        cursor=final["page"]["next_cursor"],
        limit=1,
    )
    committed = store.get_store_checkpoint("store_bootstrap")

    assert first["meta"]["checkpoint_advanced"] is False
    assert pending["cursor"] is None
    assert pending["pending_cursor"] == "bootstrap-page-2"
    assert client.calls[0]["cursor"] is None
    assert client.calls[1]["cursor"] == "bootstrap-page-2"
    assert before_ack["cursor"] is None
    assert before_ack["traversal_cursor"] == "bootstrap-page-2"
    assert final["meta"]["checkpoint_advanced"] is False
    assert ack["meta"]["delivery_acknowledged"] is True
    assert committed["cursor"] == "bootstrap-final"
    assert committed["pending_cursor"] is None
    assert {ref["id"] for ref in committed["compact_refs"]} == {"order-1", "quote-2"}


def test_digest_rejects_arbitrary_checkpoint_stream(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    client = _DigestClient([])

    result = StoreIntegration(client=client, store=store).digest(stream="attacker-controlled")

    assert result["status"] == "blocked"
    assert result["summary"]["error_code"] == "invalid_store_digest_stream"
    assert client.calls == []


def test_management_action_runs_contract_pre_read_and_dry_run_with_correlation(tmp_path):
    before = {
        "entity": "store_order",
        "id": "order-7",
        "status": "IN_PROGRESS",
        "updated_at": "2026-07-16T10:00:00+07:00",
    }
    client = _ActionClient(before=before)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_order",
        action="mark_order_ready",
        target_id="order-7",
        planned_changes={"status": "READY"},
        owner_intent="Переведи точный заказ order-7 в READY",
        expected_updated_at=before["updated_at"],
        idempotency_key="order-7-ready-v1",
        correlation_id="contract:order-7-ready-v1",
        mode="dry_run",
    )

    assert result["ok"] is True
    assert client.context_calls == 1
    assert client.action_calls[0]["correlation_id"] == "contract:order-7-ready-v1"
    assert client.action_calls[0]["mode"] == "dry_run"
    assert "store_order_ready_may_notify_customer" in result["warnings"]
    assert result["meta"]["post_state_read"] is False


def test_management_apply_requires_exact_readback_match(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_QUOTE",
        "updated_at": "version-1",
    }
    client = _ActionClient(
        before=before,
        after={**before, "status": "WAITING_FOR_APPROVAL", "updated_at": "version-2"},
    )
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="set_quote_request_status",
        target_id="quote-1",
        planned_changes={"status": "WAITING_FOR_APPROVAL"},
        owner_intent="Переведи точную заявку quote-1 в работу",
        expected_updated_at="version-1",
        idempotency_key="quote-1-progress-v1",
        correlation_id="contract:quote-1-progress-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert result["meta"]["post_state_read"] is True
    assert result["meta"]["readback_verified"] is True
    assert client.context_calls == 2


def test_management_sends_and_verifies_the_same_canonical_store_changes(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_QUOTE",
        "updated_at": "version-1",
    }
    client = _ActionClient(
        before=before,
        after={**before, "status": "WAITING_FOR_APPROVAL", "updated_at": "version-2"},
    )
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="set_quote_request_status",
        target_id=" quote-1 ",
        planned_changes={"status": " waiting_for_approval "},
        owner_intent="  Переведи точную заявку quote-1 в работу  ",
        expected_updated_at="version-1",
        idempotency_key=" quote-1-progress-canonical-v1 ",
        correlation_id="contract:quote-1-progress-canonical-v1",
        mode="apply",
    )

    assert result["ok"] is True
    sent = client.action_calls[0]
    assert sent["target_id"] == "quote-1"
    assert sent["owner_intent"] == "Переведи точную заявку quote-1 в работу"
    assert sent["idempotency_key"] == "quote-1-progress-canonical-v1"
    assert sent["planned_changes"] == {"status": "WAITING_FOR_APPROVAL"}
    assert result["meta"]["readback_verified"] is True


def test_management_apply_enters_compensating_when_readback_mismatches(tmp_path):
    before = {
        "entity": "store_batch",
        "id": "batch-1",
        "storage_location": "A-1",
        "updated_at": "version-1",
    }
    client = _ActionClient(before=before, after={**before, "storage_location": "A-1", "updated_at": "version-2"})
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_batch",
        action="set_batch_storage_location",
        target_id="batch-1",
        planned_changes={"storage_location": "B-2"},
        owner_intent="Измени место партии batch-1 на B-2",
        expected_updated_at="version-1",
        idempotency_key="batch-1-location-v1",
        correlation_id="contract:batch-1-location-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["failed_fields"] == ["storage_location"]


def test_management_dry_run_blocks_stale_version_before_write(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "internal_comment": "",
        "updated_at": "current-version",
    }
    client = _ActionClient(before=before)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="update_quote_request_comment",
        target_id="quote-1",
        planned_changes={"internal_comment": "Проверить VIN"},
        owner_intent="Обнови внутренний комментарий точной заявки quote-1",
        expected_updated_at="stale-version",
        idempotency_key="quote-1-comment-v1",
        correlation_id="contract:quote-1-comment-v1",
        mode="dry_run",
    )

    assert result["status"] == "conflict"
    assert result["summary"]["error_code"] == "store_expected_updated_at_mismatch"
    assert client.action_calls == []


def test_management_apply_replays_original_request_when_preread_revision_is_already_advanced(tmp_path):
    current = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_APPROVAL",
        "updated_at": "version-2",
    }
    replay_result = {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {"idempotency_replay": True},
    }
    client = _ActionClient(before=current, after=current, action_result=replay_result)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="set_quote_request_status",
        target_id="quote-1",
        planned_changes={"status": "WAITING_FOR_APPROVAL"},
        owner_intent="Повтори исходный запрос после потерянного ответа",
        expected_updated_at="version-1",
        idempotency_key="quote-1-progress-original-v1",
        correlation_id="contract:quote-1-progress-original-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert client.context_calls == 2
    assert len(client.action_calls) == 1
    assert client.action_calls[0]["expected_updated_at"] == "version-1"
    assert client.action_calls[0]["idempotency_key"] == "quote-1-progress-original-v1"
    assert result["meta"]["idempotency_replay"] is True
    assert result["meta"]["pre_read_revision_mismatch"] is True
    assert result["meta"]["readback_verified"] is True


def test_management_apply_with_stale_preread_and_no_receipt_returns_app_conflict(tmp_path):
    current = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_APPROVAL",
        "updated_at": "version-2",
    }
    conflict = {
        "ok": False,
        "format": "store_agent_v1",
        "status": "conflict",
        "summary": {"error_code": "store_expected_updated_at_mismatch"},
        "items": [],
        "changes": [],
        "page": {},
        "warnings": ["store_expected_updated_at_mismatch"],
        "meta": {"request_dispatched": True, "outcome_uncertain": False, "http_status": 409},
    }
    client = _ActionClient(before=current, action_result=conflict)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="set_quote_request_status",
        target_id="quote-1",
        planned_changes={"status": "WAITING_FOR_APPROVAL"},
        owner_intent="Повтори исходный запрос без сохраненной квитанции",
        expected_updated_at="version-1",
        idempotency_key="quote-1-progress-missing-receipt-v1",
        correlation_id="contract:quote-1-progress-missing-receipt-v1",
        mode="apply",
    )

    assert result["status"] == "conflict"
    assert result["summary"]["error_code"] == "store_expected_updated_at_mismatch"
    assert client.context_calls == 1
    assert len(client.action_calls) == 1
    assert result["meta"]["pre_read_revision_mismatch"] is True


def test_management_readback_maps_assignment_to_assigned_user_id(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "assigned_user_id": "employee-1",
        "updated_at": "version-1",
    }
    after = {**before, "assigned_user_id": "employee-7", "updated_at": "version-2"}
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="assign_quote_request",
        target_id="quote-1",
        planned_changes={"assignee_id": "employee-7"},
        owner_intent="Назначь точную заявку quote-1 сотруднику employee-7",
        expected_updated_at="version-1",
        idempotency_key="quote-1-assign-v1",
        correlation_id="contract:quote-1-assign-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert result["meta"]["readback_verified"] is True


def test_management_readback_verifies_internal_comment_by_canonical_hash_only(tmp_path):
    comment = "Проверить VIN"
    comment_hash = hashlib.sha256(f"comment:{comment}".encode()).hexdigest()
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "internal_comment_sha256": hashlib.sha256(b"none:").hexdigest(),
        "updated_at": "version-1",
    }
    after = {**before, "internal_comment_sha256": comment_hash, "updated_at": "version-2"}
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="update_quote_request_comment",
        target_id="quote-1",
        planned_changes={"internal_comment": f"  {comment}  "},
        owner_intent="Обнови внутренний комментарий точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-comment-hash-v1",
        correlation_id="contract:quote-1-comment-hash-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert "internal_comment" not in after
    assert result["meta"]["readback_verified"] is True


def test_management_readback_verifies_quote_note_by_exact_manager_origin(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "notes": [],
        "updated_at": "version-1",
    }
    after = {
        **before,
        "notes": [{"origin": "AUTOSTOP_MANAGER", "text": "Уточнить сторону установки"}],
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="add_quote_request_note",
        target_id="quote-1",
        planned_changes={"text": "Уточнить сторону установки"},
        owner_intent="Добавь внутреннюю заметку к точной заявке quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-note-v1",
        correlation_id="contract:quote-1-note-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert result["meta"]["readback_verified"] is True


@pytest.mark.parametrize(
    ("has_estimate_draft", "items_has_more", "expected_status", "expected_error"),
    [
        (None, False, "blocked", "store_estimate_draft_state_unavailable"),
        (True, False, "conflict", "store_estimate_draft_conflict"),
        (False, True, "blocked", "store_quote_items_incomplete"),
    ],
)
def test_quote_draft_write_requires_safe_complete_estimate_projection(
    tmp_path,
    has_estimate_draft,
    items_has_more,
    expected_status,
    expected_error,
):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": items_has_more,
        "updated_at": "version-1",
    }
    if has_estimate_draft is not None:
        before["has_estimate_draft"] = has_estimate_draft
    client = _ActionClient(before=before)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={
            "items": [
                {
                    "item_id": "item-1",
                    "drafts": [
                        {
                            "candidate_key": "rossko:abc",
                            "part_name": "Фильтр",
                            "sale_price": 1300,
                            "source_kind": "ROSSKO",
                            "price_basis": "CONFIRMED_PURCHASE",
                        }
                    ],
                }
            ]
        },
        owner_intent="Замени черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-drafts-guard-v1",
        correlation_id="contract:quote-1-drafts-guard-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert result["summary"]["error_code"] == expected_error
    assert client.action_calls == []


def test_management_readback_handles_missing_offer_lists_as_failed_verification(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": False,
        "has_estimate_draft": False,
        "updated_at": "version-1",
    }
    after = {
        **before,
        "items": [{"item_id": "item-1", "offers": None}],
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={
            "items": [
                {
                    "item_id": "item-1",
                    "drafts": [
                        {
                            "candidate_key": "rossko:abc",
                            "part_name": "Фильтр",
                            "sale_price": 1300,
                            "source_kind": "ROSSKO",
                            "price_basis": "CONFIRMED_PURCHASE",
                        }
                    ],
                }
            ]
        },
        owner_intent="Замени черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-drafts-v1",
        correlation_id="contract:quote-1-drafts-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["failed_fields"] == ["items"]


@pytest.mark.parametrize(("observed_price", "verified"), [(1300, True), (1400, False)])
def test_management_readback_verifies_quote_draft_fields(tmp_path, observed_price, verified):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": False,
        "has_estimate_draft": False,
        "updated_at": "version-1",
    }
    draft = {
        "candidate_key": "rossko:abc",
        "part_name": "Фильтр",
        "part_sku": "HU-7008Z",
        "brand": "MANN",
        "purchase_price": 1000,
        "sale_price": 1300,
        "delivery_days": 2,
        "comment": "В наличии у поставщика",
        "source_kind": "ROSSKO",
        "price_basis": "CONFIRMED_PURCHASE",
        "is_recommended": True,
    }
    after = {
        **before,
        "items": [
            {
                "item_id": "item-1",
                "offers": [
                    {
                        "supplier": None,
                        "source_ref": None,
                        "source_url": None,
                        "availability": None,
                        "fitment_confidence": "UNVERIFIED",
                        "oem_reference": None,
                        **draft,
                        "sale_price": observed_price,
                        "offer_id": "offer-1",
                        "origin": "AUTOSTOP_MANAGER",
                        "publication_status": "DRAFT",
                        "is_selected": False,
                    }
                ],
            }
        ],
        "items_has_more": False,
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={"items": [{"item_id": "item-1", "drafts": [draft]}]},
        owner_intent="Замени черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-drafts-v1",
        correlation_id="contract:quote-1-drafts-v1",
        mode="apply",
    )

    assert result["meta"]["readback_verified"] is verified
    assert result["summary"].get("failed_fields", []) == ([] if verified else ["items"])


@pytest.mark.parametrize(
    ("field", "unplanned_value"),
    [
        ("part_sku", "HU-7008Z"),
        ("brand", "MANN"),
        ("supplier", "ROSSKO"),
        ("purchase_price", 1000),
        ("delivery_days", 2),
        ("comment", "Старый комментарий"),
        ("source_ref", "rossko:abc"),
        ("source_url", "https://supplier.invalid/offer"),
        ("availability", "В наличии"),
        ("fitment_confidence", "HIGH"),
        ("oem_reference", "11428507683"),
        ("is_recommended", True),
    ],
)
def test_management_readback_rejects_unplanned_quote_draft_values(tmp_path, field, unplanned_value):
    draft = {
        "candidate_key": "rossko:abc",
        "part_name": "Фильтр",
        "sale_price": 1300,
        "source_kind": "ROSSKO",
        "price_basis": "PUBLIC_RETAIL",
    }
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": False,
        "has_estimate_draft": False,
        "updated_at": "version-1",
    }
    observed_draft = {
        "part_sku": None,
        "brand": None,
        "supplier": None,
        "purchase_price": None,
        "delivery_days": None,
        "comment": None,
        "source_ref": None,
        "source_url": None,
        "availability": None,
        "fitment_confidence": "UNVERIFIED",
        "oem_reference": None,
        "is_recommended": False,
        **draft,
        field: unplanned_value,
        "offer_id": "offer-1",
        "origin": "AUTOSTOP_MANAGER",
        "publication_status": "DRAFT",
        "is_selected": False,
    }
    after = {
        **before,
        "items": [{"item_id": "item-1", "offers": [observed_draft]}],
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={"items": [{"item_id": "item-1", "drafts": [draft]}]},
        owner_intent="Замени черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key=f"quote-1-unplanned-{field}-v1",
        correlation_id=f"contract:quote-1-unplanned-{field}-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["failed_fields"] == ["items"]
    assert result["meta"]["readback_verified"] is False


def test_management_readback_rejects_quote_draft_without_offer_id(tmp_path):
    draft = {
        "candidate_key": "rossko:abc",
        "part_name": "Фильтр",
        "sale_price": 1300,
        "source_kind": "ROSSKO",
        "price_basis": "PUBLIC_RETAIL",
    }
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": False,
        "has_estimate_draft": False,
        "updated_at": "version-1",
    }
    after = {
        **before,
        "items": [
            {
                "item_id": "item-1",
                "offers": [
                    {
                        "part_sku": None,
                        "brand": None,
                        "supplier": None,
                        "purchase_price": None,
                        "delivery_days": None,
                        "comment": None,
                        "source_ref": None,
                        "source_url": None,
                        "availability": None,
                        "fitment_confidence": "UNVERIFIED",
                        "oem_reference": None,
                        "is_recommended": False,
                        **draft,
                        "origin": "AUTOSTOP_MANAGER",
                        "publication_status": "DRAFT",
                        "is_selected": False,
                    }
                ],
            }
        ],
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={"items": [{"item_id": "item-1", "drafts": [draft]}]},
        owner_intent="Замени черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-missing-offer-id-v1",
        correlation_id="contract:quote-1-missing-offer-id-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["failed_fields"] == ["items"]
    assert result["meta"]["readback_verified"] is False


@pytest.mark.parametrize(
    ("after_items", "items_has_more", "has_estimate_draft"),
    [
        (
            [
                {"item_id": "item-1", "offers": []},
                {
                    "item_id": "item-2",
                    "offers": [
                        {
                            "candidate_key": "manager:unexpected",
                            "origin": "AUTOSTOP_MANAGER",
                            "publication_status": "DRAFT",
                        }
                    ],
                },
            ],
            False,
            False,
        ),
        ([], False, False),
        ([{"item_id": "item-1", "offers": []}], True, False),
        ([{"item_id": "item-1", "offers": []}], False, True),
    ],
)
def test_management_readback_requires_complete_quote_draft_post_state(
    tmp_path,
    after_items,
    items_has_more,
    has_estimate_draft,
):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "items": [{"item_id": "item-1", "offers": []}],
        "items_has_more": False,
        "has_estimate_draft": False,
        "updated_at": "version-1",
    }
    after = {
        **before,
        "items": after_items,
        "items_has_more": items_has_more,
        "has_estimate_draft": has_estimate_draft,
        "updated_at": "version-2",
    }
    client = _ActionClient(before=before, after=after)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="replace_quote_offer_drafts",
        target_id="quote-1",
        planned_changes={"items": [{"item_id": "item-1", "drafts": []}]},
        owner_intent="Очисти черновики предложений точной заявки quote-1",
        expected_updated_at="version-1",
        idempotency_key="quote-1-clear-drafts-v1",
        correlation_id="contract:quote-1-clear-drafts-v1",
        mode="apply",
    )

    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["failed_fields"] == ["items"]


def test_management_apply_unknown_outcome_always_rereads_and_enters_compensating(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_QUOTE",
        "updated_at": "version-1",
    }
    unknown = {
        "ok": False,
        "format": "store_agent_v1",
        "status": "degraded",
        "summary": {"error_code": "store_timeout_or_network_error"},
        "items": [],
        "changes": [],
        "page": {},
        "warnings": ["store_timeout_or_network_error"],
        "meta": {"request_dispatched": True, "outcome_uncertain": True},
    }

    for suffix, after, expected_failed in (
        ("matching", {**before, "status": "WAITING_FOR_APPROVAL", "updated_at": "version-2"}, []),
        ("mismatching", {**before, "updated_at": "version-2"}, ["status"]),
    ):
        client = _ActionClient(before=before, after=after, action_result=unknown)
        integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / suffix))
        result = integration.management_action(
            domain="store_quote_request",
            action="set_quote_request_status",
            target_id="quote-1",
            planned_changes={"status": "WAITING_FOR_APPROVAL"},
            owner_intent="Переведи точную заявку quote-1 в работу",
            expected_updated_at="version-1",
            idempotency_key=f"quote-1-progress-{suffix}-v1",
            correlation_id=f"contract:quote-1-progress-{suffix}-v1",
            mode="apply",
        )

        assert result["ok"] is False
        assert result["status"] == "compensating"
        assert result["summary"]["error_code"] == "store_apply_outcome_uncertain"
        assert result["summary"]["write_applied_unverified"] is True
        assert result["summary"]["failed_fields"] == expected_failed
        assert result["meta"]["request_dispatched"] is True
        assert result["meta"]["outcome_uncertain"] is True
        assert result["meta"]["post_state_read"] is True
        assert client.context_calls == 2
        assert len(client.action_calls) == 1


def test_management_blocks_missing_or_mismatched_pre_read_identity_and_version(tmp_path):
    cases = [
        (
            {"entity": "store_quote_request", "id": "other-quote", "updated_at": "version-1"},
            "store_preflight_target_mismatch",
        ),
        (
            {"entity": "store_quote_request", "id": "quote-1"},
            "store_preflight_version_missing",
        ),
    ]

    for before, expected_error in cases:
        client = _ActionClient(before=before)
        integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / expected_error))

        result = integration.management_action(
            domain="store_quote_request",
            action="set_quote_request_status",
            target_id="quote-1",
            planned_changes={"status": "WAITING_FOR_APPROVAL"},
            owner_intent="Переведи точную заявку quote-1 в работу",
            expected_updated_at="version-1",
            idempotency_key=f"quote-1-{expected_error}",
            correlation_id=f"contract:quote-1-{expected_error}",
            mode="apply",
        )

        assert result["status"] == "blocked"
        assert result["summary"]["error_code"] == expected_error
        assert client.action_calls == []


def test_management_apply_requires_matching_target_and_advanced_version(tmp_path):
    before = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_QUOTE",
        "updated_at": "version-1",
    }
    cases = [
        (
            {
                **before,
                "id": "other-quote",
                "status": "WAITING_FOR_APPROVAL",
                "updated_at": "version-2",
            },
            "store_apply_readback_target_mismatch",
        ),
        (
            {**before, "status": "WAITING_FOR_APPROVAL"},
            "store_apply_readback_version_not_advanced",
        ),
    ]

    for after, expected_error in cases:
        client = _ActionClient(before=before, after=after)
        integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / expected_error))

        result = integration.management_action(
            domain="store_quote_request",
            action="set_quote_request_status",
            target_id="quote-1",
            planned_changes={"status": "WAITING_FOR_APPROVAL"},
            owner_intent="Переведи точную заявку quote-1 в работу",
            expected_updated_at="version-1",
            idempotency_key=f"quote-1-{expected_error}",
            correlation_id=f"contract:quote-1-{expected_error}",
            mode="apply",
        )

        assert result["status"] == "compensating"
        assert result["summary"]["error_code"] == expected_error


def test_management_apply_accepts_same_version_only_for_idempotency_replay(tmp_path):
    state = {
        "entity": "store_quote_request",
        "id": "quote-1",
        "status": "WAITING_FOR_APPROVAL",
        "updated_at": "version-2",
    }
    replay_result = {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {"idempotency_replay": True},
    }
    client = _ActionClient(before=state, after=state, action_result=replay_result)
    integration = StoreIntegration(client=client, store=ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    result = integration.management_action(
        domain="store_quote_request",
        action="set_quote_request_status",
        target_id="quote-1",
        planned_changes={"status": "WAITING_FOR_APPROVAL"},
        owner_intent="Повтори точное ранее выполненное действие для quote-1",
        expected_updated_at="version-2",
        idempotency_key="quote-1-progress-replay-v1",
        correlation_id="contract:quote-1-progress-replay-v1",
        mode="apply",
    )

    assert result["ok"] is True
    assert result["meta"]["idempotency_replay"] is True
    assert result["meta"]["readback_verified"] is True


def test_store_checkpoint_cas_rejects_stale_writer_and_never_persists_raw_payload(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = ManagerMemoryStore(db_path)
    _seed_checkpoint(store)
    pending = store.record_store_checkpoint_pending(
        stream="store_digest",
        next_cursor="resume-1",
        compact_refs=[{"entity": "store_order", "id": "order-1", "version": "v1"}],
        baseline=False,
        expected_state_version=1,
        request_cursor="fixed-window-start",
        snapshot_at="2026-07-16T10:00:00+07:00",
        delivery_token="a" * 64,
    )
    stale = store.commit_store_checkpoint(
        stream="store_digest",
        cursor="wrong-final",
        last_success_at="2026-07-16T11:00:00+07:00",
        compact_refs=[],
        expected_state_version=1,
    )

    assert pending["ok"] is True
    assert stale["error"] == "store_checkpoint_state_conflict"
    assert b"customer_phone" not in db_path.read_bytes()


def test_scoped_store_checkpoint_reset_requires_cas_and_rebaselines_only_selected_stream(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _seed_checkpoint(store, stream="store_digest", cursor="digest-old-epoch")
    _seed_checkpoint(store, stream="store_bootstrap", cursor="bootstrap-stable")

    stale = store.reset_store_checkpoint_for_rebaseline(
        stream="store_digest",
        expected_state_version=0,
        reason="cursor_generation_mismatch",
    )
    reset = store.reset_store_checkpoint_for_rebaseline(
        stream="store_digest",
        expected_state_version=1,
        reason="cursor_generation_mismatch",
    )
    invalid_stream = store.reset_store_checkpoint_for_rebaseline(
        stream="other",
        expected_state_version=0,
        reason="operator_verified_rebaseline",
    )

    digest = store.get_store_checkpoint("store_digest")
    bootstrap = store.get_store_checkpoint("store_bootstrap")
    assert stale["error"] == "store_checkpoint_state_conflict"
    assert reset["ok"] is True
    assert reset["rebaseline_required"] is True
    assert invalid_stream["error"] == "store_checkpoint_stream_invalid"
    assert digest["cursor"] is None
    assert digest["pending_cursor"] is None
    assert digest["traversal_cursor"] is None
    assert digest["last_attempt_status"] == "reset"
    assert bootstrap["cursor"] == "bootstrap-stable"
