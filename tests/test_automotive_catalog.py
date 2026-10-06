from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_identity, automotive_labor, automotive_parts
from autostop_manager.automotive_catalog import build_bundle, content_hash, read_document, validate_registry
from autostop_manager.catalog_clients import PARTSAPI_METHOD_KEY_ENV_NAMES, PARTSAPI_OPERATIONS, partsapi_catalog_lookup
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog(tmp_path):
    server = FastMCP("catalog-test")
    register_manager_tools(server, StoreState(tmp_path / "schema.sqlite3"))
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    registry = json.loads((ROOT / "docs/agent/automotive_tools.json").read_text())
    return registry, schemas


def test_actual_registration_matches_independent_ast_and_all_provider_methods(catalog):
    registry, schemas = catalog
    tree = ast.parse((ROOT / "autostop_manager/mcp_tools.py").read_text())
    names = {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "tool"
        for keyword in node.keywords
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
    }
    atomic = ast.parse((ROOT / "autostop_manager/automotive_mcp.py").read_text())
    names.update(
        keyword.value.value
        for node in ast.walk(atomic)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "tool"
        for keyword in node.keywords
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
    )
    assignment = next(
        node
        for node in ast.walk(atomic)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "tools" for target in node.targets)
    )
    names.update(row.elts[0].id for row in assignment.value.elts)
    assert names == set(schemas)
    report = validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)
    assert report["ok"] and report["partsapi_methods"] == 43
    assert {row["tool_name"] for row in registry["native_inventory"]} == names
    assert {module["element_id"] for module in registry["modules"]} == {f"E{i}" for i in range(1, 16)}


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("missing_native", "native_inventory_mismatch"),
        ("missing_method", "partsapi_method_coverage_mismatch"),
        ("bad_alias", "partsapi_alias_mismatch"),
        ("bad_ref", "invalid_instruction_ref"),
        ("duplicate_id", "duplicate_tool_id"),
        ("bad_provider", "invalid_aftermarket_provider"),
        ("planned_invocation", "planned_invocation"),
    ],
)
def test_catalog_refuses_incomplete_or_misleading_executable_contracts(catalog, mutation, error):
    registry, schemas = catalog
    broken = deepcopy(registry)
    if mutation == "missing_native":
        broken["native_inventory"].pop()
    elif mutation == "missing_method":
        broken["tools"] = [row for row in broken["tools"] if row["tool_id"] != "partsapi.getCars"]
    elif mutation == "bad_alias":
        next(row for row in broken["tools"] if row["tool_id"] == "partsapi.getArticle")["invocation"]["api_method"] = (
            "wrong"
        )
    elif mutation == "bad_ref":
        broken["modules"][0]["instruction_ref"] = "../../etc/passwd"
    elif mutation == "duplicate_id":
        broken["tools"].append(deepcopy(broken["tools"][0]))
    elif mutation == "bad_provider":
        next(row for row in broken["tools"] if row["tool_id"] == "aftermarket.mann")["invocation"]["provider"] = (
            "unknown"
        )
    else:
        next(row for row in broken["tools"] if row["implementation_state"] == "planned")["invocation"] = {
            "transport": "manager_mcp",
            "tool_name": "decode_vin_vpic",
        }
    with pytest.raises(ValueError, match=error):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


def test_bundle_is_self_contained_hashes_content_separately_and_keeps_shared_ids(catalog):
    registry, schemas = catalog
    bundle = build_bundle(ROOT, registry, schemas, PARTSAPI_OPERATIONS, "a" * 40)
    assert bundle == build_bundle(ROOT, registry, schemas, PARTSAPI_OPERATIONS, "a" * 40)
    other_revision = build_bundle(ROOT, registry, schemas, PARTSAPI_OPERATIONS, "b" * 40)
    assert bundle["content_hash"] == other_revision["content_hash"]
    mutated = deepcopy(bundle)
    mutated["tools"][0]["instruction_text"] += "changed"
    assert content_hash(mutated) != bundle["content_hash"]
    modules = {m["element_id"]: m for m in bundle["modules"]}
    assert "partsapi.getArticleCriteria" in modules["E6"]["tool_ids"]
    assert "partsapi.getArticleCriteria" in modules["E4"]["tool_ids"]
    assert sum(t["tool_id"] == "partsapi.getArticleCriteria" for t in bundle["tools"]) == 1
    assert all(m["instruction_text"] and m["instruction_hash"] for m in bundle["modules"])
    assert "tool_statuses" not in bundle


def test_documents_cannot_escape_the_snapshot(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    root = tmp_path / "source"
    root.mkdir()
    (root / "link.md").symlink_to(outside)
    with pytest.raises(ValueError, match="invalid_instruction_ref"):
        read_document(root, "link.md")


@pytest.mark.parametrize(
    "target,field,value,error",
    [
        ("partsapi.getArticle", "input_fields", ["missing"], "provider_parameter_contract_mismatch"),
        ("partsapi.getArticle", "defaults", {"invented": 1}, "provider_parameter_contract_mismatch"),
        ("manager.inspect_vehicle_identifier", "input_fields", ["missing"], "native_parameter_contract_mismatch"),
        ("manager.inspect_vehicle_identifier", "defaults", {"identifier": "wrong"}, "native_default_contract_mismatch"),
    ],
)
def test_catalog_rejects_signature_and_provider_contract_drift(catalog, target, field, value, error):
    registry, schemas = catalog
    broken = deepcopy(registry)
    next(row for row in broken["tools"] if row["tool_id"] == target)[field] = value
    with pytest.raises(ValueError, match=error):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


@pytest.mark.parametrize("target,selector", [("partsapi.getArticle", "operation"), ("aftermarket.mann", "provider")])
def test_catalog_rejects_example_for_a_different_provider_selector(catalog, target, selector):
    registry, schemas = catalog
    broken = deepcopy(registry)
    next(row for row in broken["tools"] if row["tool_id"] == target)["example"][selector] = "wrong"
    with pytest.raises(ValueError, match="example_selector_mismatch"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


def test_catalog_rejects_ambiguous_provider_ownership(catalog):
    registry, schemas = catalog
    broken = deepcopy(registry)
    broken["providers"].append(deepcopy(broken["providers"][0]))
    with pytest.raises(ValueError, match="duplicate_provider_id"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


def test_pure_helper_documentation_examples_execute_without_acquisition(catalog, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("instruction example attempted acquisition")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("autostop_manager.work_pricing._load_labor_experience", forbidden)
    registry, _ = catalog
    tested = []
    modules = (automotive_identity, automotive_parts, automotive_labor)
    for tool in registry["tools"]:
        name = tool.get("invocation", {}).get("tool_name") if tool.get("invocation") else None
        implementation = next(
            (getattr(module, name, None) for module in modules if name and hasattr(module, name)), None
        )
        if tool["execution_kind"] != "pure" or implementation is None:
            continue
        response = implementation(**tool["example"])
        assert response["outcome"] != "invalid_input", tool["tool_id"]
        assert response["execution"]["network_calls"] == 0, tool["tool_id"]
        tested.append(name)
    assert len(tested) == 10


def test_all_provider_documentation_examples_pass_dry_run_without_http(catalog, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("provider instruction example attempted HTTP")

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://example.com/api")
    for name in PARTSAPI_METHOD_KEY_ENV_NAMES.values():
        monkeypatch.setenv(name, "synthetic-key")
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", forbidden)
    registry, _ = catalog
    tested = []
    for tool in registry["tools"]:
        if not tool["tool_id"].startswith("partsapi."):
            continue
        assert tool["example"]["dry_run"] is True
        response = partsapi_catalog_lookup(**tool["example"])
        assert response["outcome"] == "configured_unverified", tool["tool_id"]
        assert response["attempt_count"] == 0
        tested.append(tool["tool_id"])
    assert len(tested) == 43
