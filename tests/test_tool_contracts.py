from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.tool_contracts import audit_tool_contract_registry, build_tool_contract_registry


ROOT = Path(__file__).resolve().parents[1]


def test_every_declared_manager_tool_has_a_complete_operational_contract():
    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))

    audit = audit_tool_contract_registry(catalog["all_tools"])
    registry = build_tool_contract_registry(catalog["all_tools"])

    assert audit["ok"] is True
    assert audit["tool_count"] == catalog["tool_count"] == 56
    assert audit["incomplete_tools"] == []
    assert set(registry) == set(catalog["all_tools"])
    assert all(contract["input_schema"].startswith("FastMCP") for contract in registry.values())


def test_mutating_and_external_write_plan_tools_are_explicitly_classified():
    names = ["remember", "memory_review_apply", "prepare_crm_card_action", "partsapi_catalog_lookup"]
    registry = build_tool_contract_registry(names)

    assert registry["remember"]["operation_kind"] == "local_write"
    assert "readback" in str(registry["remember"]["post_write_verification"])
    assert registry["memory_review_apply"]["risk"] == "medium"
    assert registry["prepare_crm_card_action"]["operation_kind"] == "write_plan"
    assert "separate CRM authorization" in str(registry["prepare_crm_card_action"]["authorization"])
    assert registry["partsapi_catalog_lookup"]["operation_kind"] == "read"
    assert "provider_credentials" in str(registry["partsapi_catalog_lookup"]["authorization"])
