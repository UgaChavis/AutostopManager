from __future__ import annotations

import json

import pytest

from autostop_manager.storage import StoreState
from autostop_manager.store_change_feed_contract import STORE_CHANGE_FEED_ENTITY_TYPES
from autostop_manager.store_integration import StoreIntegration


@pytest.mark.parametrize("stream", ["store_digest", "store_bootstrap"])
def test_all_feed_entity_refs_survive_pending_ack_and_checkpoint_reopen(tmp_path, stream):
    db_path = tmp_path / "checkpoint.sqlite3"
    store = StoreState(db_path)
    refs = [
        {"entity": entity, "id": f"fixture-{index}", "updated_at": "2026-11-06T10:00:00Z"}
        for index, entity in enumerate(sorted(STORE_CHANGE_FEED_ENTITY_TYPES))
    ]

    pending = store.record_store_checkpoint_pending(
        stream=stream,
        next_cursor="page-next",
        compact_refs=refs,
        page_refs=refs,
        baseline=False,
        expected_state_version=0,
        request_cursor="window-start",
        delivery_token="a" * 64,
        page_limit=100,
    )
    assert pending["ok"] is True
    checkpoint = StoreState(db_path).get_store_checkpoint(stream)
    assert checkpoint["cursor"] is None
    assert checkpoint["pending_refs"] == refs
    assert checkpoint["pending_page_refs"] == refs

    acknowledged = store.acknowledge_store_checkpoint_page(
        stream=stream,
        cursor="page-next",
        delivery_token="a" * 64,
        expected_state_version=checkpoint["state_version"],
    )
    assert acknowledged["ok"] is True
    checkpoint = StoreState(db_path).get_store_checkpoint(stream)
    assert checkpoint["cursor"] is None
    assert checkpoint["pending_refs"] == []
    assert checkpoint["traversal_refs"] == refs

    committed = store.commit_store_checkpoint(
        stream=stream,
        cursor="window-final",
        last_success_at="2026-11-06T10:01:00Z",
        compact_refs=refs,
        expected_state_version=checkpoint["state_version"],
    )
    assert committed["ok"] is True
    reopened = StoreState(db_path).get_store_checkpoint(stream)
    assert reopened["cursor"] == "window-final"
    assert reopened["compact_refs"] == refs
    assert reopened["traversal_refs"] == []


@pytest.mark.parametrize(
    "ref",
    [
        {"entity": "store_unknown_entity", "id": "fixture-policy"},
        {"entity": "store_marketplace_publication_policy", "id": "unsafe identifier"},
        {"entity": "store_marketplace_publication_policy", "id": "fixture-policy", "deleted": True},
        {
            "entity": "store_marketplace_publication_policy",
            "id": "fixture-policy",
            "owner_read": {"source_table": "marketplace_publication_policies"},
        },
        {"entity": "store_customer", "id": "fixture-customer", "phone": "+79990000000"},
    ],
)
def test_feed_checkpoints_reject_unknown_entities_and_payload_metadata(tmp_path, ref):
    store = StoreState(tmp_path / "checkpoint.sqlite3")
    committed = store.commit_store_checkpoint(
        stream="store_digest",
        cursor="must-not-advance",
        last_success_at="2026-11-06T10:01:00Z",
        compact_refs=[ref],
        expected_state_version=0,
    )
    pending = store.record_store_checkpoint_pending(
        stream="store_digest",
        next_cursor="must-not-advance",
        compact_refs=[],
        page_refs=[ref],
        baseline=False,
        expected_state_version=0,
        delivery_token="a" * 64,
    )

    assert committed["ok"] is False
    assert pending["ok"] is False
    assert store.get_store_checkpoint()["exists"] is False


def test_feed_entity_ref_does_not_expand_workflow_evidence_or_active_run_projection(tmp_path):
    store = StoreState(tmp_path / "checkpoint.sqlite3")
    started = store.start_workflow_run(
        workflow_id="store_management",
        intent="store_management",
        idempotency_key="fixture-run",
    )
    assert started["ok"] is True
    run_id = started["id"]
    ref = {"entity": "store_marketplace_publication_policy", "id": "fixture-policy"}

    rejected = store.checkpoint_workflow_run(run_id, checkpoint={"compact_refs": [ref]})
    assert rejected["ok"] is False
    assert rejected["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "compact_refs[0]" in rejected["forbidden_keys"]

    # Previously persisted or imported workflow rows must retain the same
    # narrow read boundary, independently of the digest checkpoint contract.
    with store.connect() as conn:
        conn.execute(
            "UPDATE manager_runs SET checkpoint_json = ? WHERE id = ?",
            (json.dumps({"compact_refs": [ref]}), run_id),
        )
    active = store.list_active_manager_runs()
    assert active["items"][0]["checkpoint"] == {}

    legacy_ref = {"entity": "store_part", "id": "fixture-part"}
    accepted = store.checkpoint_workflow_run(run_id, checkpoint={"compact_refs": [legacy_ref]})
    assert accepted["ok"] is True
    active = store.list_active_manager_runs()
    assert active["items"][0]["checkpoint"]["compact_refs"] == [legacy_ref]


@pytest.mark.parametrize("with_pending", [False, True])
def test_generation_mismatch_preserves_delivered_cursor_and_pending_ack(tmp_path, with_pending):
    store = StoreState(tmp_path / "checkpoint.sqlite3")
    refs = [{"entity": "store_marketplace_publication_policy", "id": "fixture-policy"}]
    assert store.commit_store_checkpoint(
        stream="store_digest",
        cursor="previous-generation-delivered",
        last_success_at="2026-11-06T10:00:00Z",
        compact_refs=refs,
        expected_state_version=0,
    )["ok"]
    if with_pending:
        assert store.record_store_checkpoint_pending(
            stream="store_digest",
            next_cursor="previous-generation-final",
            compact_refs=refs,
            page_refs=refs,
            baseline=False,
            expected_state_version=1,
            request_cursor="previous-generation-window",
            delivery_token="a" * 64,
            page_has_more=False,
            snapshot_at="2026-11-06T10:01:00Z",
        )["ok"]
    before = store.get_store_checkpoint()

    class GenerationMismatchClient:
        def __init__(self):
            self.calls = []

        def digest(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": False,
                "status": "degraded",
                "summary": {"error_code": "AGENT_CURSOR_GENERATION_MISMATCH"},
                "meta": {"reset_required": True},
            }

    client = GenerationMismatchClient()
    failed = StoreIntegration(client=client, store=store).digest()
    after = StoreState(store.path).get_store_checkpoint()

    assert failed["ok"] is False
    assert failed["meta"]["checkpoint_advanced"] is False
    assert failed["meta"]["delivery_acknowledged"] is False
    assert len(client.calls) == 1
    assert client.calls[0]["baseline"] is False
    expected_cursor = "previous-generation-window" if with_pending else "previous-generation-delivered"
    assert client.calls[0]["cursor"] == expected_cursor
    for key in before:
        if key not in {"last_attempt_status", "last_error_code", "state_version", "updated_at"}:
            assert after[key] == before[key], key
    assert after["last_attempt_status"] == "degraded"
    assert after["last_error_code"] == "agent_cursor_generation_mismatch"
