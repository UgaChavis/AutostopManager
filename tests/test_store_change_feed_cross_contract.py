"""Opt-in Store serializer -> Manager delivery contract rehearsal.

Set AUTOSTOP_STORE_CONTRACT_ROOT to a Store checkout and
AUTOSTOP_STORE_CONTRACT_PYTHON to its Python environment. Normal Manager CI
does not import Store or require its dependencies. Both databases are synthetic.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest

import autostop_manager.store_api as store_api_module
from autostop_manager.storage import STORE_LEDGER_REF_ENTITIES, StoreState, _safe_bootstrap_checkpoint
from autostop_manager.store_api import STORE_ENTITIES, STORE_MANAGEMENT_OPERATIONS, StoreApiClient
from autostop_manager.store_change_feed_contract import STORE_CHANGE_FEED_ENTITY_TYPES
from autostop_manager.store_integration import STORE_DOMAIN_ACTIONS, StoreIntegration


_STORE_PRODUCER = r"""
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "backend"))

def no_network(*args, **kwargs):
    raise AssertionError("cross-contract fixtures cannot use the network")

socket.socket.connect = no_network
socket.create_connection = no_network

# app.settings normally discovers checkout .env files. Disable dotenv before
# importing it; the child environment contains only explicit synthetic settings.
from pydantic_settings import BaseSettings
original_init = BaseSettings.__init__
def without_dotenv(self, *args, **kwargs):
    kwargs["_env_file"] = None
    original_init(self, *args, **kwargs)
BaseSettings.__init__ = without_dotenv
from app.settings import settings
BaseSettings.__init__ = original_init
assert settings.database_url == "sqlite:///:memory:"

from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from app.agent_auth import AgentPrincipalContext
from app.agent_change_feed_registry import (
    ALL_FEED_ENTITY_TYPES, COMPATIBILITY_ENTITY_TYPES, RELATED_REF_FIELDS, TABLE_BY_ENTITY_TYPE,
)
from app.db import Base
from app.models import AgentChangeClock, AgentEntityChange, Category, Manufacturer, Part
from app.routers.agent import get_digest

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
principal = AgentPrincipalContext("synthetic", "cross-contract", "synthetic", frozenset({"store:read"}))
timestamp = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
def entity_id(entity):
    return "store_state" if entity == "store_state" else "synthetic-" + entity.removeprefix("store_")

with Session(engine) as db:
    db.add_all([
        Category(id="synthetic-category", name="Synthetic category"),
        Manufacturer(id="synthetic-manufacturer", name="Synthetic manufacturer"),
    ])
    db.flush()
    db.add(Part(
        id=entity_id("store_part"), sku="SYNTHETIC-001", name="Synthetic part",
        category_id="synthetic-category", manufacturer_id="synthetic-manufacturer",
        price_retail=100, price_wholesale=90, stock_qty=0, updated_at=timestamp,
    ))
    db.commit()
    db.execute(delete(AgentEntityChange))
    clock = db.get(AgentChangeClock, 1)
    clock.generation = "11111111-1111-4111-8111-111111111111"
    clock.current_sequence = 0
    db.commit()
    baseline = get_digest(baseline=True, since=None, cursor=None, limit=5, db=db, principal=principal)
    for index, entity in enumerate(sorted(ALL_FEED_ENTITY_TYPES), 1):
        deleted = entity in COMPATIBILITY_ENTITY_TYPES - {"store_part"} or entity == "store_service_principal_credential"
        relations = RELATED_REF_FIELDS.get(TABLE_BY_ENTITY_TYPE.get(entity), ())
        relation_args = {}
        for prefix, (related_entity, _) in zip(("parent", "related"), relations):
            relation_args[prefix + "_entity_type"] = related_entity
            relation_args[prefix + "_entity_id"] = entity_id(related_entity)
        db.add(AgentEntityChange(
            change_id=index, transaction_id=index, entity_type=entity,
            entity_id=entity_id(entity), is_deleted=deleted,
            change_kind="deleted" if deleted else "updated", changed_at=timestamp,
            **relation_args,
        ))
    clock.current_sequence = len(ALL_FEED_ENTITY_TYPES)
    db.commit()
    pages = {}
    cursor = baseline["page"]["next_cursor"]
    while True:
        page = get_digest(baseline=False, since=None, cursor=cursor, limit=5, db=db, principal=principal)
        pages[cursor] = page
        replay_cursor = page["page"]["replay_cursor"]
        if replay_cursor:
            pages[replay_cursor] = get_digest(
                baseline=False, since=None, cursor=replay_cursor, limit=5, db=db, principal=principal,
            )
        cursor = page["page"]["next_cursor"]
        if not page["page"]["has_more"]:
            break
    print(json.dumps(jsonable_encoder({
        "baseline": baseline, "pages": pages, "final_cursor": cursor,
        "entity_types": sorted(ALL_FEED_ENTITY_TYPES),
    })))
engine.dispose()
"""


@pytest.fixture(scope="module")
def serialized_store_feed(tmp_path_factory):
    root = os.environ.get("AUTOSTOP_STORE_CONTRACT_ROOT")
    python = os.environ.get("AUTOSTOP_STORE_CONTRACT_PYTHON")
    if not root or not python:
        pytest.skip("requires explicit Store checkout and Python environment for cross-contract rehearsal")
    assert (Path(root) / "backend" / "app" / "agent_read.py").is_file()
    assert Path(python).is_file()
    scratch = tmp_path_factory.mktemp("store-cross-contract")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    environment.update(
        {
            "DATABASE_URL": "sqlite:///:memory:",
            "APP_RUNTIME_MODE": "isolated",
            "ROSSKO_ENABLED": "false",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = subprocess.run(
        [python, "-B", "-c", _STORE_PRODUCER, str(Path(root).resolve())],
        cwd=scratch,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class _Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}


def _client():
    return StoreApiClient(
        api_url="http://store.invalid/internal/agent/v1",
        read_token="synthetic-only",
        manage_token="",
    )


def test_real_store_feed_survives_validation_replay_and_ack(serialized_store_feed, monkeypatch, tmp_path):
    fixture = serialized_store_feed
    assert STORE_CHANGE_FEED_ENTITY_TYPES == set(fixture["entity_types"])
    requests = []
    for page in fixture["pages"].values():
        _client()._validate_envelope(
            page,
            expected_item_limit=5,
            expected_change_limit=5,
            response_contract="digest",
            expected_entity=None,
            expected_detail=None,
            expected_operation=None,
        )

    def transport(request, **_kwargs):
        assert request.get_method() == "GET"
        query = parse_qs(urlparse(request.full_url).query)
        requests.append(query)
        if query["baseline"] == ["true"]:
            page = fixture["baseline"]
        else:
            page = fixture["pages"][query["cursor"][0]]
        return _Response(json.dumps(page).encode("utf-8"))

    monkeypatch.setattr(store_api_module, "urlopen", transport)
    state = StoreState(tmp_path / "manager.sqlite3")
    integration = StoreIntegration(client=_client(), store=state)
    baseline = integration.digest(limit=5)
    assert baseline["ok"] is True, baseline
    assert baseline["status"] == "baseline"
    start_cursor = state.get_store_checkpoint()["cursor"]
    delivery = integration.digest(limit=5)
    observed = []
    page_count = 0
    while True:
        assert delivery["ok"] is True, delivery
        assert delivery["page"]["ack_required"] is True
        assert state.get_store_checkpoint()["cursor"] == start_cursor
        # Replay must survive process-local client/state replacement: only the
        # synthetic SQLite checkpoint is carried forward.
        state = StoreState(tmp_path / "manager.sqlite3")
        integration = StoreIntegration(client=_client(), store=state)
        replay = integration.digest(limit=5)
        assert replay["ok"] is True, replay
        assert replay["items"] == delivery["items"]
        assert replay["page"]["ack_token"] == delivery["page"]["ack_token"]
        assert replay["meta"]["replayed_delivery"] is True
        observed.extend(delivery["items"])
        page_count += 1
        source_has_more = delivery["page"]["source_has_more"]
        delivery = integration.digest(cursor=replay["page"]["next_cursor"], limit=5)
        if not source_has_more:
            break
        assert page_count <= len(fixture["entity_types"])

    assert delivery["ok"] is True, delivery
    assert delivery["meta"]["delivery_acknowledged"] is True
    assert delivery["items"] == []
    checkpoint = state.get_store_checkpoint()
    assert checkpoint["cursor"] == fixture["final_cursor"]
    assert checkpoint["pending_cursor"] is None
    assert {ref["entity"] for ref in checkpoint["compact_refs"]} == set(fixture["entity_types"])
    assert all(set(ref) <= {"entity", "id", "version", "updated_at"} for ref in checkpoint["compact_refs"])
    assert len(requests) == 1 + page_count * 2
    by_entity = {item["entity"]: item for item in observed}
    assert (
        by_entity["store_marketplace_publication_policy"]["owner_read"]["source_table"]
        == "marketplace_publication_policies"
    )
    assert by_entity["store_part"]["name"] == "Synthetic part"
    assert by_entity["store_order"]["deleted"] is True
    assert by_entity["store_order"]["change_kind"] == "deleted"
    assert by_entity["store_state"]["counts"]["parts"] == 1
    assert "failed_marketplace_export_jobs" in by_entity["store_state"]["counts"]
    for entity in ("store_audit_log", "store_service_principal", "store_service_principal_credential"):
        assert by_entity[entity]["safe_ref_only"] is True
        assert "owner_read" not in by_entity[entity]
    assert by_entity["store_service_principal_credential"]["deleted"] is True
    persisted = (tmp_path / "manager.sqlite3").read_bytes()
    assert b"Synthetic part" not in persisted
    assert b"owner_read" not in persisted


def test_feed_delivery_does_not_expand_entity_or_workflow_evidence_permissions():
    compatibility = {
        "store_part",
        "store_order",
        "store_quote_request",
        "store_batch",
        "store_warehouse_operation",
        "store_marketplace_listing",
        "store_state",
    }
    assert STORE_ENTITIES == compatibility | {"store_sourcing_offer"}
    assert STORE_LEDGER_REF_ENTITIES == compatibility
    expected_actions = {
        ("store_quote_request", "assign_quote_request"),
        ("store_quote_request", "set_quote_request_status"),
        ("store_quote_request", "update_quote_request_comment"),
        ("store_quote_request", "add_quote_request_note"),
        ("store_batch", "set_batch_storage_location"),
        ("store_order", "mark_order_ready"),
        ("store_order", "set_order_payment_status"),
    }
    assert STORE_DOMAIN_ACTIONS == expected_actions
    assert STORE_MANAGEMENT_OPERATIONS == {operation for _, operation in expected_actions}
    policy_ref = {"entity": "store_marketplace_publication_policy", "id": "synthetic-policy"}
    assert "compact_refs" not in _safe_bootstrap_checkpoint({"compact_refs": [policy_ref]})
    with pytest.raises(ValueError, match="unsupported store entity"):
        _client().entity_context(entity=policy_ref["entity"], entity_id=policy_ref["id"])
