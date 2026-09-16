from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from autostop_manager import crm_parts_store
from autostop_manager.config import CrmMcpConnectionConfig
from autostop_manager.crm_parts_store import PARTS_STORE_COLUMN, parts_store_cards


class FakeCrmGateway:
    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {
            "other-1": {
                "id": "other-1",
                "column": "inbox",
                "title": "Other",
                "vehicle": "",
                "description": "Unrelated",
                "updated_at": "r1",
            },
        }
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def invoke(self, name: str, arguments: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]:
        self.calls.append((name, arguments, idempotency_key))
        if name == "search_cards":
            cards = [card for card in self.cards.values() if card["column"] == arguments["column"]]
            return {"ok": True, "data": {"data": {"cards": cards, "meta": {"has_more": False}}}}
        if name == "get_card":
            card = self.cards.get(arguments["card_id"])
            return {"ok": bool(card), "data": {"data": {"card": dict(card)}} if card else {}}
        if name == "create_card":
            card = {"id": "store-1", **arguments, "updated_at": "r1"}
            self.cards["store-1"] = card
            return {"ok": True, "data": {"data": {"card": dict(card)}}}
        if name == "update_card":
            card = self.cards[arguments["card_id"]]
            if card["updated_at"] != arguments["expected_updated_at"]:
                return {"ok": False}
            card.update(description=arguments["description"], updated_at="r2")
            return {"ok": True, "data": {"data": {"card": dict(card)}}}
        raise AssertionError(name)


def test_parts_store_card_create_list_append_and_other_column_is_rejected():
    gateway = FakeCrmGateway()
    created = parts_store_cards(
        "create",
        transport=gateway,
        title="Synthetic part inquiry",
        description="Initial facts",
        idempotency_key="create-synthetic-1",
    )
    assert created["ok"] is True
    assert created["card"]["column"] == PARTS_STORE_COLUMN
    assert gateway.calls[0] == (
        "create_card",
        {
            "title": "Synthetic part inquiry",
            "vehicle": "",
            "description": "Initial facts",
            "column": PARTS_STORE_COLUMN,
        },
        "create-synthetic-1",
    )

    listed = parts_store_cards("list", transport=gateway)
    assert [card["id"] for card in listed["items"]] == ["store-1"]
    assert gateway.calls[-1][1]["column"] == PARTS_STORE_COLUMN

    appended = parts_store_cards(
        "append_note",
        transport=gateway,
        card_id="store-1",
        note="Next step",
        expected_updated_at="r1",
        idempotency_key="append-synthetic-1",
    )
    assert appended["ok"] is True
    assert appended["card"]["description"] == "Initial facts\n\nNext step"
    assert gateway.calls[-2] == (
        "update_card",
        {"card_id": "store-1", "description": "Initial facts\n\nNext step", "expected_updated_at": "r1"},
        "append-synthetic-1",
    )
    writes_before = len([name for name, _, _ in gateway.calls if name == "update_card"])
    assert (
        parts_store_cards(
            "append_note",
            transport=gateway,
            card_id="store-1",
            note="Again",
            expected_updated_at="r1",
            idempotency_key="append-synthetic-2",
        )["error"]
        == "card_revision_changed"
    )
    assert (
        parts_store_cards(
            "append_note",
            transport=gateway,
            card_id="other-1",
            note="Do not write",
            idempotency_key="append-synthetic-3",
        )["error"]
        == "card_outside_parts_store"
    )
    assert len([name for name, _, _ in gateway.calls if name == "update_card"]) == writes_before


def test_loopback_transport_uses_live_schema_and_preserves_full_get_card_shape(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    @asynccontextmanager
    async def fake_stream(_url, *, http_client):
        yield object(), object(), lambda: None

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments, **_kwargs):
            calls.append((name, arguments))
            if name == "get_raw_capability_schema":
                capability = arguments["name"]
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "ok": True,
                        "summary": {
                            "name": capability,
                            "risk": "write" if capability == "create_card" else "read",
                            "schema_hash": "a" * 16,
                        },
                        "data": {"input_schema": {"type": "object"}},
                    },
                )
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "ok": True,
                    "data": {
                        "ok": True,
                        "data": {
                            "card": {
                                "id": "store-1",
                                "column": PARTS_STORE_COLUMN,
                                "title": "Synthetic",
                                "vehicle": "",
                                "description": "A" * 5000,
                                "updated_at": "r1",
                            }
                        },
                    },
                },
            )

    monkeypatch.setattr(crm_parts_store.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(crm_parts_store, "streamable_http_client", fake_stream)
    monkeypatch.setattr(crm_parts_store, "ClientSession", FakeSession)
    transport = crm_parts_store.LoopbackCrmPartsStoreTransport(
        CrmMcpConnectionConfig(
            configured=True,
            url="http://127.0.0.1:8001/mcp",
            bearer_token="synthetic-token",
        )
    )
    read = transport.invoke("get_card", {"card_id": "store-1"})
    assert crm_parts_store._card(read)["description"] == "A" * 5000
    assert calls[1][1]["allow_large_output"] is True
    transport.invoke(
        "create_card", {"title": "Synthetic", "column": PARTS_STORE_COLUMN}, idempotency_key="synthetic-create-1"
    )
    assert calls[3][1]["idempotency_key"] == "synthetic-create-1"
    assert calls[3][1]["arguments"]["column"] == PARTS_STORE_COLUMN


def test_parts_store_create_exposes_only_safe_gateway_failure_code():
    class BlockedGateway:
        def invoke(self, _name, _arguments, *, idempotency_key=""):
            return {"ok": False, "status": "blocked", "warnings": ["agent_gateway_writes_disabled"]}

    result = parts_store_cards(
        "create", transport=BlockedGateway(), title="Synthetic", idempotency_key="create-synthetic-2"
    )
    assert result["ok"] is False
    assert result["gateway_warning"] == "agent_gateway_writes_disabled"
    assert result["outcome_uncertain"] is False
