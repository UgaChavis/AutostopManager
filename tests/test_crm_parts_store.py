from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

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


def test_parts_store_note_suffix_is_not_a_complete_history_block():
    gateway = FakeCrmGateway()
    parts_store_cards(
        "create",
        transport=gateway,
        title="Synthetic",
        description="Предложение не отправлено",
        idempotency_key="create-synthetic-suffix",
    )

    result = parts_store_cards(
        "append_note",
        transport=gateway,
        card_id="store-1",
        note="отправлено",
        idempotency_key="append-synthetic-suffix",
    )

    assert result["status"] == "completed"
    assert result["card"]["description"] == "Предложение не отправлено\n\nотправлено"
    assert len([name for name, _, _ in gateway.calls if name == "update_card"]) == 1


def test_parts_store_note_repeated_as_whole_block_is_already_present():
    for history in ("Final note", "Earlier facts\n\nFinal note"):
        gateway = FakeCrmGateway()
        parts_store_cards(
            "create",
            transport=gateway,
            title="Synthetic",
            description=history,
            idempotency_key="create-synthetic-whole-block",
        )

        result = parts_store_cards(
            "append_note",
            transport=gateway,
            card_id="store-1",
            note="Final note",
            idempotency_key="append-synthetic-whole-block",
        )

        assert result["status"] == "already_present"
        assert result["card"]["description"] == history
        assert not any(name == "update_card" for name, _, _ in gateway.calls)


def test_parts_store_create_requires_readback_of_the_created_id():
    class WrongReadbackGateway(FakeCrmGateway):
        def invoke(self, name, arguments, *, idempotency_key=""):
            result = super().invoke(name, arguments, idempotency_key=idempotency_key)
            if name == "get_card" and result.get("ok") is True:
                result["data"]["data"]["card"]["id"] = "store-another"
            return result

    result = parts_store_cards(
        "create",
        transport=WrongReadbackGateway(),
        title="Synthetic",
        description="Synthetic facts",
        idempotency_key="create-synthetic-wrong-target",
    )

    assert result["ok"] is False
    assert result["error"] == "crm_create_unverified"
    assert result["outcome_uncertain"] is True


def _create_with_transport(gateway, *, native, monkeypatch, tmp_path, **arguments):
    if not native:
        return parts_store_cards("create", transport=gateway, **arguments)

    from mcp.server.fastmcp import FastMCP

    from autostop_manager import mcp_tools
    from autostop_manager.mcp_store_executor import StoreWorkers
    from autostop_manager.mcp_telemetry import instrument_manager_tools
    from autostop_manager.storage import StoreState

    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    monkeypatch.setattr(
        crm_parts_store, "get_crm_mcp_connection_config", lambda: CrmMcpConnectionConfig(configured=False)
    )
    monkeypatch.setattr(crm_parts_store, "LoopbackCrmPartsStoreTransport", lambda _config: gateway)
    server = FastMCP("synthetic-crm-create-readback")
    server.store_workers = StoreWorkers()
    mcp_tools.register_manager_tools(
        server, store=StoreState(tmp_path / "unused.sqlite3"), include_tools={"parts_store_cards"}
    )
    instrument_manager_tools(server)
    try:
        tool = server._tool_manager.get_tool("parts_store_cards")
        return asyncio.run(tool.run({"operation": "create", **arguments}))
    finally:
        server.store_workers.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("actual_vehicle", ["", "Honda Civic", None, 42, {}, "missing-field"])
def test_parts_store_create_rejects_unverified_vehicle(tmp_path, monkeypatch, native, actual_vehicle):
    class ChangedVehicleGateway(FakeCrmGateway):
        def invoke(self, name, arguments, *, idempotency_key=""):
            result = super().invoke(name, arguments, idempotency_key=idempotency_key)
            if name == "create_card":
                # Change after the successful create, before Manager's independent readback.
                if actual_vehicle == "missing-field":
                    self.cards["store-1"].pop("vehicle")
                else:
                    self.cards["store-1"]["vehicle"] = actual_vehicle
            return result

    gateway = ChangedVehicleGateway()
    result = _create_with_transport(
        gateway,
        native=native,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        title="Synthetic",
        vehicle="Toyota Camry",
        description="Synthetic facts",
        idempotency_key="create-synthetic-vehicle",
    )

    assert result["ok"] is False
    assert result["status"] == "uncertain"
    assert result["error"] == "crm_create_unverified"
    assert result["outcome_uncertain"] is True
    assert [name for name, _, _ in gateway.calls] == ["create_card", "get_card"]
    assert gateway.calls[0][2] == "create-synthetic-vehicle"
    if native:
        assert result["tool_execution"]["outcome"] == "error"


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("vehicle", [None, "", "Toyota Camry", " Toyota  Camry ", "Toyota\t\nCamry\u00a0", " \t\n"])
def test_parts_store_create_accepts_canonical_or_default_vehicle(tmp_path, monkeypatch, native, vehicle):
    class CanonicalVehicleGateway(FakeCrmGateway):
        def invoke(self, name, arguments, *, idempotency_key=""):
            result = super().invoke(name, arguments, idempotency_key=idempotency_key)
            if name == "create_card":
                canonical = " ".join(arguments["vehicle"].split())
                self.cards["store-1"]["vehicle"] = canonical
                result["data"]["data"]["card"]["vehicle"] = canonical
            return result

    gateway = CanonicalVehicleGateway()
    arguments = {} if vehicle is None else {"vehicle": vehicle}
    result = _create_with_transport(
        gateway,
        native=native,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        title="Synthetic",
        idempotency_key="create-synthetic-canonical-vehicle",
        **arguments,
    )

    assert result["ok"] is True
    assert result["card"]["vehicle"] == " ".join((vehicle or "").split())
    # Readback normalization must not alter the existing write body/idempotency binding.
    assert gateway.calls[0][1]["vehicle"] == (vehicle or "").strip()
    assert [name for name, _, _ in gateway.calls] == ["create_card", "get_card"]


@pytest.mark.parametrize("vehicle", [None, 42, True, {}, []])
def test_parts_store_native_create_rejects_non_string_vehicle(tmp_path, monkeypatch, vehicle):
    from mcp.server.fastmcp.exceptions import ToolError

    gateway = FakeCrmGateway()
    with pytest.raises(ToolError):
        _create_with_transport(
            gateway,
            native=True,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            title="Synthetic",
            vehicle=vehicle,
            idempotency_key="create-synthetic-invalid-vehicle",
        )
    assert gateway.calls == []


def test_parts_store_append_requires_readback_of_the_requested_id():
    class WrongReadbackGateway(FakeCrmGateway):
        def invoke(self, name, arguments, *, idempotency_key=""):
            result = super().invoke(name, arguments, idempotency_key=idempotency_key)
            write_occurred = any(call_name == "update_card" for call_name, _, _ in self.calls)
            if name == "get_card" and write_occurred and result.get("ok") is True:
                result["data"]["data"]["card"]["id"] = "store-another"
            return result

    gateway = WrongReadbackGateway()
    parts_store_cards(
        "create",
        transport=gateway,
        title="Synthetic",
        description="Synthetic facts",
        idempotency_key="create-synthetic-append-target",
    )
    result = parts_store_cards(
        "append_note",
        transport=gateway,
        card_id="store-1",
        note="Synthetic next step",
        idempotency_key="append-synthetic-wrong-target",
    )

    assert result["ok"] is False
    assert result["error"] == "crm_append_unverified"
    assert result["outcome_uncertain"] is True
