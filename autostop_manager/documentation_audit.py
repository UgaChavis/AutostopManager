from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)]+)\)")
TRACKED_DOC_REF_RE = re.compile(r"(?<![\w/])(docs/agent/[A-Za-z0-9_./-]+\.(?:md|jsonl|json))")


def audit_documentation(project_root: Path | str = PROJECT_ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    docs_root = root / "docs" / "agent"
    markdown_files = sorted([root / "README.md", root / "AGENTS.md", *docs_root.rglob("*.md")])
    markdown_files = [path for path in markdown_files if path.is_file()]

    broken_links: list[str] = []
    broken_doc_refs: list[str] = []
    for path in markdown_files:
        text = path.read_text(encoding="utf-8-sig")
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not _inside_root(resolved, root) or not resolved.exists():
                broken_links.append(f"{path.relative_to(root)} -> {raw_target}")
        for raw_target in TRACKED_DOC_REF_RE.findall(text):
            if not (root / raw_target).is_file():
                broken_doc_refs.append(f"{path.relative_to(root)} -> {raw_target}")

    knowledge_map = _read_json(docs_root / "knowledge_map.json")
    command_routes = _read_json(docs_root / "command_routes.json")
    manager_rules = _read_json(docs_root / "manager_rules.json")
    annotations = _read_jsonl(docs_root / "knowledge_annotations.jsonl")
    manager_catalog = _read_json(docs_root / "manager_mcp_catalog.json")

    domains = (knowledge_map.get("domains") or {}) if isinstance(knowledge_map, dict) else {}
    routes = (command_routes.get("routes") or []) if isinstance(command_routes, dict) else []
    rules = (manager_rules.get("rules") or []) if isinstance(manager_rules, dict) else []
    all_tools = (manager_catalog.get("all_tools") or []) if isinstance(manager_catalog, dict) else []

    duplicate_ids = {
        "command_routes": _duplicates(str(item.get("command_id") or "") for item in routes if isinstance(item, dict)),
        "manager_rules": _duplicates(str(item.get("id") or "") for item in rules if isinstance(item, dict)),
        "annotations": _duplicates(
            str(item.get("annotation_id") or "") for item in annotations if isinstance(item, dict)
        ),
        "manager_tools": _duplicates(str(item) for item in all_tools),
    }

    route_errors: list[str] = []
    for item in routes:
        if not isinstance(item, dict):
            route_errors.append("command_routes contains a non-object item")
            continue
        command_id = str(item.get("command_id") or "<missing>")
        domain = str(item.get("domain") or "")
        open_first = str(item.get("open_first") or "")
        if domain not in domains:
            route_errors.append(f"{command_id}: unknown domain {domain}")
        if not open_first or not (root / open_first).is_file():
            route_errors.append(f"{command_id}: missing open_first {open_first}")

    annotation_errors: list[str] = []
    for item in annotations:
        if not isinstance(item, dict):
            annotation_errors.append("knowledge_annotations contains a non-object line")
            continue
        annotation_id = str(item.get("annotation_id") or "<missing>")
        domain = str(item.get("domain") or "")
        annotation_path = str(item.get("path") or "")
        if domain not in domains:
            annotation_errors.append(f"{annotation_id}: unknown domain {domain}")
        if not annotation_path or not (root / annotation_path).is_file():
            annotation_errors.append(f"{annotation_id}: missing path {annotation_path}")

    invalid_json_files = sorted(
        name
        for name, payload in {
            "knowledge_map.json": knowledge_map,
            "command_routes.json": command_routes,
            "manager_rules.json": manager_rules,
            "manager_mcp_catalog.json": manager_catalog,
        }.items()
        if not isinstance(payload, dict)
    )
    duplicate_values = {name: values for name, values in duplicate_ids.items() if values}
    warnings = [
        *("broken_markdown_links" for _ in [0] if broken_links),
        *("broken_tracked_doc_references" for _ in [0] if broken_doc_refs),
        *("duplicate_identifiers" for _ in [0] if duplicate_values),
        *("route_contract_errors" for _ in [0] if route_errors),
        *("annotation_contract_errors" for _ in [0] if annotation_errors),
        *("invalid_json_documents" for _ in [0] if invalid_json_files),
    ]
    return {
        "ok": not warnings,
        "markdown_files_checked": len(markdown_files),
        "broken_links": sorted(set(broken_links)),
        "broken_doc_refs": sorted(set(broken_doc_refs)),
        "duplicate_ids": duplicate_values,
        "route_errors": route_errors,
        "annotation_errors": annotation_errors,
        "invalid_json_files": invalid_json_files,
        "warnings": warnings,
    }


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return []
    items: list[Any] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append(None)
    return items


def _duplicates(values: Any) -> list[str]:
    normalized = [str(value) for value in values if str(value)]
    counts = Counter(normalized)
    return sorted(value for value, count in counts.items() if count > 1)
