from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams
import pytest

from autostop_manager import config, j1_vin_store, mcp_tools
from autostop_manager.mcp_probe import _effectful_annotation_mismatches
from autostop_manager.storage import StoreState


VIN_TOOLS = {"j1_research_vin", "j1_research_record_facts", "j1_research_document"}
SYNTHETIC_VIN = "1HGCM82673A000000"


@pytest.fixture
def native_server(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    for name in (
        "AUTOSTOP_STORE_API_URL",
        "AUTOSTOP_STORE_READ_TOKEN",
        "AUTOSTOP_STORE_MANAGE_TOKEN",
        "AUTOSTOP_STORE_OWNER_TOKEN",
        "AUTOSTOP_STORE_QUOTE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    server = FastMCP("isolated-j1-vin-contract-test")
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "state.sqlite3"), include_tools=VIN_TOOLS)
    return server


async def _call(server, name, arguments):
    handler = server._mcp_server.request_handlers[CallToolRequest]
    result = await handler(CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments)))
    return result.root


def test_vin_tools_are_registered_while_disabled_with_effectful_annotations(native_server):
    tools = {tool.name: tool for tool in asyncio.run(native_server.list_tools())}
    assert set(tools) == VIN_TOOLS
    for name, open_world in (
        ("j1_research_vin", True),
        ("j1_research_record_facts", False),
        ("j1_research_document", False),
    ):
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is open_world
    assert _effectful_annotation_mismatches(list(tools.values())) == []
    assert tools["j1_research_vin"].inputSchema["required"] == ["vin", "idempotency_key"]
    assert set(tools["j1_research_vin"].inputSchema["properties"]) == {"vin", "idempotency_key"}


def test_disabled_vin_tools_reject_without_starting_domain_work(native_server, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled VIN tools must not access private jobs or providers")

    # TTL cleanup is separately tested by the core; isolate this wire call from
    # the host runtime and prove it cannot create or change a private job.
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(j1_vin_store, "create", forbidden)
    monkeypatch.setattr(j1_vin_store, "connect", forbidden)
    monkeypatch.setattr(mcp_tools, "decode_vehicle_identity_async", forbidden)
    requests = (
        ("j1_research_vin", {"vin": SYNTHETIC_VIN, "idempotency_key": "disabled-start"}),
        (
            "j1_research_record_facts",
            {
                "job_id": "0" * 32,
                "expected_revision": 0,
                "facts": [],
                "idempotency_key": "disabled-facts",
            },
        ),
    )
    for name, arguments in requests:
        response = asyncio.run(_call(native_server, name, arguments))
        assert response.isError is False
        assert response.structuredContent["ok"] is False
        assert response.structuredContent["error"]["code"] == "vin_research_disabled"


def test_vin_start_and_cited_facts_use_independent_domain_functions(native_server, monkeypatch):
    calls = []

    def record_start(**kwargs):
        calls.append(("start", kwargs))
        return {"ok": True, "job_id": "vin-test-job", "analysis_status": "awaiting_agent"}

    def record_facts(**kwargs):
        calls.append(("facts", kwargs))
        return {"ok": True, "revision": 3, "analysis_status": "ready_partial"}

    def forbidden_decoder(*args, **kwargs):
        raise AssertionError("VIN research must not call a decoder")

    monkeypatch.setattr(mcp_tools, "j1_research_vin", record_start)
    monkeypatch.setattr(mcp_tools, "j1_research_record_facts", record_facts)
    monkeypatch.setattr(mcp_tools, "decode_vehicle_identity_async", forbidden_decoder)
    start = asyncio.run(
        _call(native_server, "j1_research_vin", {"vin": SYNTHETIC_VIN, "idempotency_key": "start-test"})
    )
    assert start.isError is False
    assert start.structuredContent["analysis_status"] == "awaiting_agent"
    fact = {
        "field": "engine",
        "value": "Synthetic engine",
        "unit": "",
        "document_id": "1" * 32,
        "document_revision": 1,
        "quote": "Synthetic engine",
        "page": 456,
        "relationship": "family",
        "derivation": "direct",
        "reasoning": "Family specification only",
        "basis_claim_ids": [],
        "match_evidence_id": None,
    }
    arguments = {
        "job_id": "vin-test-job",
        "expected_revision": 2,
        "facts": [fact],
        "idempotency_key": "facts-test",
        "finalize": True,
    }
    recorded = asyncio.run(_call(native_server, "j1_research_record_facts", arguments))
    assert recorded.isError is False
    assert recorded.structuredContent["analysis_status"] == "ready_partial"
    assert calls == [
        ("start", {"vin": SYNTHETIC_VIN, "idempotency_key": "start-test"}),
        ("facts", arguments),
    ]


@pytest.mark.parametrize(
    "changed",
    [
        {"expected_revision": True},
        {"expected_revision": "2"},
        {"expected_revision": -1},
        {"finalize": "false"},
        {"finalize": 1},
        {"facts": "[]"},
    ],
)
def test_claim_wire_types_are_checked_before_domain_mutation(native_server, monkeypatch, changed):
    calls = []
    monkeypatch.setattr(mcp_tools, "j1_research_record_facts", lambda **kwargs: calls.append(kwargs))
    arguments = {
        "job_id": "vin-test-job",
        "expected_revision": 2,
        "facts": [],
        "idempotency_key": "facts-test",
        **changed,
    }
    result = asyncio.run(_call(native_server, "j1_research_record_facts", arguments))
    assert result.isError is True
    assert calls == []


@pytest.mark.parametrize("options", [{"page": True}, {"page": "456"}, {"page": 0}, {"page": 1001}, {"ocr": "false"}])
def test_document_page_options_reject_invalid_wire_types(native_server, monkeypatch, options):
    calls = []
    monkeypatch.setattr(mcp_tools, "research_document", lambda **kwargs: calls.append(kwargs))
    result = asyncio.run(
        _call(native_server, "j1_research_document", {"job_id": "job-test", "document_id": "doc-test", **options})
    )
    assert result.isError is True
    assert calls == []


def test_document_old_defaults_and_selected_pdf_page_reach_dispatch(native_server, monkeypatch):
    calls = []

    def document(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "document_revision": 1}

    monkeypatch.setattr(mcp_tools, "research_document", document)
    for options in ({}, {"page": 456, "ocr": True}):
        result = asyncio.run(
            _call(
                native_server,
                "j1_research_document",
                {"job_id": "job-test", "document_id": "doc-test", **options},
            )
        )
        assert result.isError is False
    assert calls == [
        {
            "job_id": "job-test",
            "document_id": "doc-test",
            "offset": 0,
            "max_chars": 8000,
            "page": None,
            "ocr": False,
        },
        {
            "job_id": "job-test",
            "document_id": "doc-test",
            "offset": 0,
            "max_chars": 8000,
            "page": 456,
            "ocr": True,
        },
    ]


def test_probe_detects_incorrect_claim_write_annotations():
    stale = SimpleNamespace(
        name="j1_research_record_facts",
        annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    assert _effectful_annotation_mismatches([stale]) == ["j1_research_record_facts"]
