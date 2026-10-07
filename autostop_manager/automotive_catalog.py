"""Validate and export the immutable automotive instruction catalogue.

Descriptions live in the registry, rules in Markdown, signatures in MCP code.
No provider is contacted and no application state is used by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .document_links import local_document_link_target
from .markdown_links import visible_markdown_links

REGISTRY_VERSION = "autostop.automotive-tools.v1"
BUNDLE_VERSION = "autostop.automotive-tools.bundle.v1"


def content_hash(payload: dict[str, Any]) -> str:
    content = {k: v for k, v in payload.items() if k not in {"content_hash", "source_revision"}}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_document(root: Path, reference: str) -> str:
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"invalid_instruction_ref:{reference}")
    return path.read_text(encoding="utf-8")


def _provider_method(tool: dict[str, Any], operations: dict[str, dict[str, Any]]) -> str | None:
    invocation = tool["invocation"]
    name = invocation["tool_name"]
    operation = invocation.get("operation")
    if name == "partsapi_catalog_lookup" and operation is not None:
        if operation not in operations:
            raise ValueError("partsapi_operation_not_registered")
        spec = operations[operation]
        if invocation["api_method"] != spec["method"]:
            raise ValueError("partsapi_alias_mismatch")
        if tool["example"].get("operation") != operation:
            raise ValueError("example_selector_mismatch")
        fields = (
            ["provider_parameters." + key for key in spec["required"]]
            if spec.get("generic_response")
            else list(spec["required"])
        )
        if tool["input_fields"] != fields or tool["defaults"] != spec.get("defaults", {}):
            raise ValueError("provider_parameter_contract_mismatch")
        return str(spec["method"])
    if name == "public_aftermarket_catalog_lookup" and invocation.get("provider") not in {"mann", "denso"}:
        raise ValueError("invalid_aftermarket_provider")
    if name == "public_aftermarket_catalog_lookup" and tool["example"].get("provider") != invocation["provider"]:
        raise ValueError("example_selector_mismatch")
    return None


def _native_parameters(tool: dict[str, Any], schema: dict[str, Any]) -> None:
    invocation = tool["invocation"]
    if invocation.get("operation") is not None and invocation["tool_name"] == "partsapi_catalog_lookup":
        return
    properties = schema["properties"]
    if any(field not in properties for field in tool["input_fields"]):
        raise ValueError("native_parameter_contract_mismatch")
    for field, value in tool["defaults"].items():
        if field not in properties or "default" not in properties[field] or properties[field]["default"] != value:
            raise ValueError("native_default_contract_mismatch")


def _documentation_examples(tool: dict[str, Any], schema: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    validator.validate(tool["example"])
    if validator.is_valid(tool["invalid_example"]):
        raise ValueError(f"invalid_example_accepted_by_schema:{tool['tool_id']}")


def _documented_invocation(tool: dict[str, Any]) -> str:
    invocation = tool["invocation"]
    if invocation is None:
        return "`Внешняя зависимость, без invocation`"
    parts = [invocation["tool_name"]]
    parts.extend(f"{key}={invocation[key]}" for key in ("operation", "provider") if key in invocation)
    return "`" + " · ".join(parts) + "`"


def _module_documentation(root: Path, module: dict[str, Any], tools: dict[str, dict[str, Any]]) -> None:
    text = read_document(root, module["instruction_ref"])
    code = module["element_id"]
    lines = text.splitlines()
    if not lines or lines[0] != f"# {code} — {module['title']}":
        raise ValueError(f"module_document_title_mismatch:{code}")
    document = root / module["instruction_ref"]
    references = set()
    for link in visible_markdown_links(text):
        target = local_document_link_target(link)
        if target is not None:
            references.add((document.parent / target).resolve())
    if (root / module["reference"]).resolve() not in references:
        raise ValueError(f"module_document_reference_mismatch:{code}")
    if code == "E1":
        return  # E1 is a task map, not a per-operation table.
    expected = {
        (root / tool["instruction_ref"]).resolve(): tool
        for tool in tools.values()
        if tool["classification"] == "active" and (tool["primary_module"] == code or code in tool["also_used_in"])
    }
    seen: set[Path] = set()
    # These curated operation tables have one fixed row format; ordinary links
    # above use the shared CommonMark parser, including reference-style links.
    for line in text.splitlines():
        row = re.fullmatch(r"\| \[([^\]]+)\]\(([^)]+)\) \| ([^|]+) \| ([^|]+) \|", line)
        if row is None:
            continue
        target = (document.parent / row[2]).resolve()
        if target not in references:
            raise ValueError(f"module_tool_table_not_visible:{code}")
        tool = expected.get(target)
        if tool is None or target in seen:
            raise ValueError(f"module_tool_table_ownership_mismatch:{code}")
        seen.add(target)
        service = [part.strip() for part in row[3].split("·")]
        if (
            row[1] != tool["title"]
            or service != [tool["provider_id"], tool["execution_kind"], tool["implementation_state"]]
            or row[4].strip() != _documented_invocation(tool)
        ):
            raise ValueError(f"module_tool_table_contract_mismatch:{code}:{tool['tool_id']}")
    if seen != set(expected):
        raise ValueError(f"module_tool_table_coverage_mismatch:{code}")


def validate_registry(
    root: Path,
    registry: dict[str, Any],
    native_schemas: dict[str, Any],
    partsapi_operations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schema = json.loads((root / "docs/agent/automotive_tools.schema.json").read_text())
    Draft202012Validator(schema).validate(registry)
    modules = {m["element_id"]: m for m in registry["modules"]}
    if len(modules) != 15 or set(modules) != {f"E{i}" for i in range(1, 16)}:
        raise ValueError("module_map_incomplete")
    if len({m["module_key"] for m in modules.values()}) != 15:
        raise ValueError("duplicate_module_key")
    providers = [p["provider_id"] for p in registry["providers"]]
    if len(providers) != len(set(providers)):
        raise ValueError("duplicate_provider_id")
    tools = {t["tool_id"]: t for t in registry["tools"]}
    if len(tools) != len(registry["tools"]):
        raise ValueError("duplicate_tool_id")
    for module in modules.values():
        read_document(root, module["instruction_ref"])
        read_document(root, module["reference"])
        if module["element_id"] != "E1" and module["parent"] != "E1":
            raise ValueError("invalid_module_parent")
    covered_methods: dict[str, str] = {}
    native_ids: dict[str, list[str]] = {}
    for tool in tools.values():
        read_document(root, tool["instruction_ref"])
        read_document(root, tool["reference"])
        if tool["classification"] == "active" and tool["primary_module"] not in modules:
            raise ValueError("invalid_primary_module")
        if any(m not in modules for m in tool["also_used_in"]):
            raise ValueError("invalid_shared_module")
        invocation = tool.get("invocation")
        if tool["implementation_state"] == "planned":
            if invocation:
                raise ValueError("planned_invocation")
            continue
        if not invocation or invocation["tool_name"] not in native_schemas:
            raise ValueError("tool_not_registered")
        name = invocation["tool_name"]
        _documentation_examples(tool, native_schemas[name])
        _native_parameters(tool, native_schemas[name])
        native_ids.setdefault(name, []).append(tool["tool_id"])
        method = _provider_method(tool, partsapi_operations)
        if method is not None:
            if method in covered_methods:
                raise ValueError("duplicate_partsapi_method")
            covered_methods[method] = tool["tool_id"]
    expected_methods = {s["method"] for s in partsapi_operations.values()}
    if set(covered_methods) != expected_methods:
        raise ValueError("partsapi_method_coverage_mismatch")
    inventory = {i["tool_name"]: i for i in registry["native_inventory"]}
    if len(inventory) != len(registry["native_inventory"]) or set(inventory) != set(native_schemas):
        raise ValueError("native_inventory_mismatch")
    for name, item in inventory.items():
        if item["tool_ids"] != sorted(native_ids.get(name, [])):
            raise ValueError(f"native_ownership_mismatch:{name}")
        read_document(root, item["reference"])
    for module in modules.values():
        _module_documentation(root, module, tools)
    return {
        "ok": True,
        "modules": len(modules),
        "tools": len(tools),
        "native_tools": len(inventory),
        "partsapi_methods": len(covered_methods),
    }


def build_bundle(
    root: Path,
    registry: dict[str, Any],
    native_schemas: dict[str, Any],
    partsapi_operations: dict[str, dict[str, Any]],
    source_revision: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("invalid_source_revision")
    validate_registry(root, registry, native_schemas, partsapi_operations)
    tools = []
    for source in registry["tools"]:
        tool = {**source, "instruction_text": read_document(root, source["instruction_ref"])}
        tool["instruction_hash"] = hashlib.sha256(tool["instruction_text"].encode()).hexdigest()
        invocation = tool.get("invocation")
        if invocation:
            tool["input_schema_ref"] = invocation["tool_name"]
        tools.append(tool)
    modules = []
    for source in registry["modules"]:
        code = source["element_id"]
        module = {**source, "instruction_text": read_document(root, source["instruction_ref"])}
        module["instruction_hash"] = hashlib.sha256(module["instruction_text"].encode()).hexdigest()
        module["tool_ids"] = [
            t["tool_id"]
            for t in tools
            if t["classification"] == "active" and (t["primary_module"] == code or code in t["also_used_in"])
        ]
        modules.append(module)
    bundle = {
        "schema_version": BUNDLE_VERSION,
        "source_revision": source_revision,
        "modules": modules,
        "tools": tools,
        "native_schemas": native_schemas,
        "native_inventory": registry["native_inventory"],
        "providers": registry["providers"],
        "migration": registry["migration"],
    }
    bundle["content_hash"] = content_hash(bundle)
    return bundle
