from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_identity, automotive_labor, automotive_parts, elcats_catalog
from autostop_manager.automotive_catalog import build_bundle, content_hash, read_document, validate_registry
from autostop_manager.automotive_contracts import public_oem_catalog_ref
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


@pytest.mark.parametrize("fault", ["provider_reference", "provider_identity", "native_classification"])
def test_export_rejects_broken_provider_navigation_and_contradictory_native_classification(catalog, fault):
    registry, schemas = catalog
    broken = deepcopy(registry)
    if fault == "provider_reference":
        broken["providers"][0]["reference"] = "docs/agent/references/missing-provider.md"
        error = "invalid_instruction_ref"
    elif fault == "provider_identity":
        broken["providers"][0]["provider_id"] = "undeclared-provider"
        error = "undeclared_provider_id"
    else:
        item = next(item for item in broken["native_inventory"] if item["classification"] == "active")
        item["classification"] = "outside"
        error = "native_classification_mismatch"
    with pytest.raises(ValueError, match=error):
        build_bundle(ROOT, broken, schemas, PARTSAPI_OPERATIONS, "a" * 40)


def test_mixed_partsapi_facade_remains_active_with_outside_operations(catalog):
    registry, schemas = catalog
    tools = {tool["tool_id"]: tool for tool in registry["tools"]}
    item = next(item for item in registry["native_inventory"] if item["tool_name"] == "partsapi_catalog_lookup")
    assert {tools[tool_id]["classification"] for tool_id in item["tool_ids"]} == {"active", "outside"}
    assert item["classification"] == "active"
    assert (
        build_bundle(ROOT, registry, schemas, PARTSAPI_OPERATIONS, "a" * 40)["native_inventory"]
        == registry["native_inventory"]
    )


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


@pytest.mark.parametrize(
    "target,selector",
    [("partsapi.getArticle", "operation"), ("aftermarket.mann", "provider"), ("elcats.list_parts", "operation")],
)
def test_catalog_rejects_example_for_a_different_provider_selector(catalog, target, selector):
    registry, schemas = catalog
    broken = deepcopy(registry)
    next(row for row in broken["tools"] if row["tool_id"] == target)["example"][selector] = (
        "resolve_vehicle" if target == "elcats.list_parts" else "wrong"
    )
    with pytest.raises(ValueError, match="example_selector_mismatch"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


def test_catalog_rejects_ambiguous_provider_ownership(catalog):
    registry, schemas = catalog
    broken = deepcopy(registry)
    broken["providers"].append(deepcopy(broken["providers"][0]))
    with pytest.raises(ValueError, match="duplicate_provider_id"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


@pytest.mark.parametrize(
    "tool_id,accepted_input",
    [
        ("manager.catalog_provider_status", {"stage": None}),
        ("manager.recommend_automotive_sources", {"brand": None}),
        ("manager.validate_partsapi_category_index", {"path": None}),
        ("manager.search_partsapi_category_index", {"query": None}),
        ("partsapi.getArticle", {"operation": "getArticle"}),
    ],
)
def test_catalog_does_not_label_schema_accepted_defaults_or_domain_inputs_negative(catalog, tool_id, accepted_input):
    registry, schemas = catalog
    broken = deepcopy(registry)
    next(tool for tool in broken["tools"] if tool["tool_id"] == tool_id)["invalid_example"] = accepted_input
    with pytest.raises(ValueError, match="invalid_example_accepted_by_schema"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


@pytest.mark.parametrize("kind", ["module", "tool"])
def test_catalog_rejects_missing_detailed_contracts(catalog, kind):
    registry, schemas = catalog
    broken = deepcopy(registry)
    broken["modules" if kind == "module" else "tools"][0]["reference"] = "docs/agent/references/missing.md"
    with pytest.raises(ValueError, match="invalid_instruction_ref"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


def test_offline_and_market_navigation_reaches_their_dedicated_contracts(catalog):
    registry, _ = catalog
    modules = {module["element_id"]: module for module in registry["modules"]}
    tools = {tool["tool_id"]: tool for tool in registry["tools"]}
    assert tools["manager.search_offline_parts_catalogs"]["primary_module"] == "E4"
    assert tools["manager.search_offline_parts_catalogs"]["reference"] == "docs/agent/references/offline-catalogs.md"
    assert (
        modules["E11"]["reference"]
        == tools["manager.assess_part_market"]["reference"]
        == ("docs/agent/references/part-market.md")
    )


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("empty", "module_document_title_mismatch"),
        ("title", "module_document_title_mismatch"),
        ("kind", "module_tool_table_contract_mismatch"),
        ("missing_row", "module_tool_table_coverage_mismatch"),
        ("duplicate_row", "module_tool_table_ownership_mismatch"),
        ("foreign_row", "module_tool_table_ownership_mismatch"),
    ],
)
def test_module_documentation_rejects_drift_from_operation_ownership(catalog, monkeypatch, mutation, error):
    registry, schemas = catalog
    reference = "docs/agent/modules/E14.md"
    text = read_document(ROOT, reference)
    row = next(line for line in text.splitlines() if "| [Маршруты исследования" in line)
    if mutation == "empty":
        text = ""
    elif mutation == "title":
        text = text.replace("# E14 — Техническая информация и диагностика", "# E14 — Старый поставщик", 1)
    elif mutation == "kind":
        text = text.replace("manager · local_read · implemented", "manager · pure · implemented", 1)
    elif mutation == "missing_row":
        text = text.replace(row + "\n", "", 1)
    elif mutation == "duplicate_row":
        text = text.replace(row, row + "\n" + row, 1)
    else:
        text = text.replace(
            "../tools/manager-recommend-automotive-sources.md", "../tools/manager-assess-part-market.md", 1
        )
    original = read_document
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    with pytest.raises(ValueError, match=error):
        validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)


@pytest.mark.parametrize("literal,card_links_elsewhere", [("code", False), ("code", True), ("html", True)])
def test_module_operation_table_inside_code_is_not_navigation(catalog, monkeypatch, literal, card_links_elsewhere):
    registry, schemas = catalog
    reference = "docs/agent/modules/E14.md"
    text = read_document(ROOT, reference)
    start = text.index("| Инструмент / карточка |")
    end = text.index("\n\n", start)
    table = text[start:end]
    opening, closing = ("```markdown", "```") if literal == "code" else ("<!--", "-->")
    text = text[:start] + opening + "\n" + table + "\n" + closing + text[end:]
    if card_links_elsewhere:
        text += "\n" + "\n".join(
            line.split(" | ")[0].removeprefix("| ") for line in table.splitlines() if line.startswith("| [")
        )
    original = read_document
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    with pytest.raises(ValueError, match="module_tool_table_not_visible"):
        validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)


def test_module_table_card_links_accept_document_line_annotations(catalog, monkeypatch):
    registry, schemas = catalog
    reference = "docs/agent/modules/E14.md"
    original = read_document
    text = original(ROOT, reference).replace(
        "../tools/manager-recommend-automotive-sources.md)", "../tools/manager-recommend-automotive-sources.md:0002)"
    )
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    assert validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)["ok"]


@pytest.mark.parametrize(
    "separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\n", "\r\n", "\r"]
)
def test_module_table_preserves_source_rows_after_literal_unicode_prose(catalog, monkeypatch, separator):
    registry, schemas = catalog
    reference = "docs/agent/modules/E14.md"
    original = read_document
    text = original(ROOT, reference).replace("\n\n", f"\n\nСправочный текст: начало{separator}конец.\n\n", 1)
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    assert validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)["ok"]


@pytest.mark.parametrize(
    "reference,old,new",
    [
        ("E13", "`calculate_work_price`", "`normalize_labor_time`"),
        ("E12", "`partsapi_catalog_lookup · operation=norms_times`", "`partsapi_catalog_lookup · operation=article`"),
        (
            "E4",
            "`public_aftermarket_catalog_lookup · provider=mann`",
            "`public_aftermarket_catalog_lookup · provider=denso`",
        ),
        ("E14", "`Внешняя зависимость, без invocation`", "`j1_research_start`"),
    ],
)
def test_module_operation_call_matches_native_facade_and_planned_contracts(catalog, monkeypatch, reference, old, new):
    registry, schemas = catalog
    reference = f"docs/agent/modules/{reference}.md"
    original = read_document
    text = original(ROOT, reference)
    assert old in text
    text = text.replace(old, new, 1)
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    with pytest.raises(ValueError, match="module_tool_table_contract_mismatch"):
        validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)


@pytest.mark.parametrize("rendered", [True, False])
def test_module_contract_navigation_uses_visible_commonmark_links(catalog, monkeypatch, rendered):
    registry, schemas = catalog
    reference = "docs/agent/modules/E1.md"
    text = read_document(ROOT, reference)
    old = "[Технический runtime](../references/manager-runtime.md)"
    if rendered:
        text = text.replace(old, "[Технический runtime][runtime]")
        text += "\n[runtime]: ../references/manager-runtime.md:0002\n"
    else:
        text = text.replace(old, "```") + old + "\n```\n"
    original = read_document
    monkeypatch.setattr(
        "autostop_manager.automotive_catalog.read_document",
        lambda root, target: text if target == reference else original(root, target),
    )
    if rendered:
        assert validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)["ok"]
    else:
        with pytest.raises(ValueError, match="module_document_reference_mismatch"):
            validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)


def test_catalog_validation_checks_all_examples_without_calling_their_tools(catalog, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("instruction validation attempted acquisition")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", forbidden)
    monkeypatch.setattr("autostop_manager.work_pricing._load_labor_experience", forbidden)
    registry, schemas = catalog
    assert validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)["tools"] == len(registry["tools"])


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


@pytest.mark.parametrize(
    "operation,kind",
    [
        ("resolve_vehicle", None),
        ("list_groups", "modification"),
        ("list_diagrams", "group"),
        ("list_parts", "diagram"),
        ("lookup_candidates", "modification"),
    ],
)
def test_elcats_documentation_examples_validate_native_refs_without_catalog_reads(
    catalog, monkeypatch, operation, kind
):
    def forbidden(*args, **kwargs):
        raise AssertionError("public catalog example attempted acquisition")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr(elcats_catalog, "PublicCatalogReader", forbidden)
    monkeypatch.setattr(elcats_catalog, "elcats_catalog_query", forbidden)
    registry, schemas = catalog
    assert validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)["ok"]
    example = next(tool["example"] for tool in registry["tools"] if tool["tool_id"] == "elcats." + operation)
    profile, conflicts, missing = elcats_catalog._profile(example["vehicle_identity"])
    assert not conflicts and not missing
    if kind is None:
        assert "catalog_ref" not in example
        return
    reference = example["catalog_ref"]
    assert public_oem_catalog_ref(reference, entity_kind=kind, vehicle_profile=profile)
    entry = next(entry for entry in elcats_catalog.elcats_catalog_entries() if entry["id"] == reference["entry_id"])
    assert elcats_catalog._ref_url(entry, reference, profile).startswith("https://www.elcats.ru/vw/")


@pytest.mark.parametrize("fault", ["wrong_kind", "missing_entry", "wrong_path", "unbound_context", "missing_parent"])
def test_elcats_catalog_gate_rejects_schema_valid_semantic_reference_errors(catalog, monkeypatch, fault):
    def forbidden(*args, **kwargs):
        raise AssertionError("semantic validation attempted acquisition")

    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr(elcats_catalog, "PublicCatalogReader", forbidden)
    monkeypatch.setattr(elcats_catalog, "elcats_catalog_query", forbidden)
    registry, schemas = catalog
    broken = deepcopy(registry)
    reference = next(tool["example"] for tool in broken["tools"] if tool["tool_id"] == "elcats.list_parts")[
        "catalog_ref"
    ]
    if fault == "wrong_kind":
        reference["entity_kind"] = "model"
    elif fault == "missing_entry":
        reference.pop("entry_id")
    elif fault == "wrong_path":
        reference["path"] = "/demo"
    elif fault == "unbound_context":
        reference["vehicle_context"]["input_binding"] = "a" * 64
    else:
        reference.pop("parent_ref")
    with pytest.raises(ValueError, match="invalid_elcats_example_reference"):
        validate_registry(ROOT, broken, schemas, PARTSAPI_OPERATIONS)


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
