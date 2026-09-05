from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import _string_list


def _default_skill_root() -> Path:
    configured = os.environ.get("CODEX_SKILL_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex" / "skills"


SKILL_ROOT = _default_skill_root()
KNOWLEDGE_MAP_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_map.json"


def _normalize_route_list_fields(route: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(route)
    normalized["primary_files"] = _string_list(normalized.get("primary_files"))
    return normalized


def _load_knowledge_map() -> dict[str, Any]:
    if not KNOWLEDGE_MAP_PATH.exists():
        return {"domains": {}, "load_error": "knowledge_map_missing"}
    try:
        payload = json.loads(KNOWLEDGE_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "domains": {},
            "load_error": "knowledge_map_unreadable" if isinstance(exc, OSError) else "knowledge_map_invalid_json",
            "load_error_detail": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "domains": {},
            "load_error": "knowledge_map_invalid_structure",
            "load_error_detail": type(payload).__name__,
        }
    domains = payload.get("domains")
    if not isinstance(domains, dict):
        return {
            **payload,
            "domains": {},
            "load_error": "knowledge_map_invalid_domains",
            "load_error_detail": type(domains).__name__,
        }
    return {
        **payload,
        "domains": {
            str(key): _normalize_route_list_fields(value) for key, value in domains.items() if isinstance(value, dict)
        },
    }


def _skill_id_from_path(path: str) -> str:
    return Path(path).parent.name if path.endswith("SKILL.md") else Path(path).name


def _knowledge_root() -> Path:
    project_root = PROJECT_ROOT.resolve(strict=False)
    map_path = KNOWLEDGE_MAP_PATH.resolve(strict=False)
    if len(map_path.parents) >= 3 and map_path.parent.name == "agent" and map_path.parent.parent.name == "docs":
        return map_path.parents[2]
    if map_path == project_root or project_root in map_path.parents:
        return project_root
    return map_path.parent


def _is_project_like_knowledge_map() -> bool:
    map_path = KNOWLEDGE_MAP_PATH.resolve(strict=False)
    return len(map_path.parents) >= 3 and map_path.parent.name == "agent" and map_path.parent.parent.name == "docs"


def _safe_resolve_skill_path(raw_path: str, *, skill_root: Path) -> dict[str, Any]:
    path = Path(raw_path).expanduser()
    knowledge_root = _knowledge_root()
    resolved = (path if path.is_absolute() else knowledge_root / path).resolve(strict=False)
    allowed_roots = [skill_root.expanduser().resolve(strict=False)]
    if _is_project_like_knowledge_map():
        allowed_roots.append(knowledge_root.resolve(strict=False))
    if any(resolved == root or root in resolved.parents for root in allowed_roots):
        return {"path": str(resolved), "unsafe_path": None}
    public_name = path.name or "SKILL.md"
    return {
        "path": f"<unsafe_skill_path>/{public_name}",
        "unsafe_path": "outside_allowed_skill_roots",
    }


def load_skill_registry(*, skill_root: Path | None = None) -> dict[str, Any]:
    root = skill_root or SKILL_ROOT
    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    by_skill: dict[str, dict[str, Any]] = {}

    for domain, route in domains.items():
        skill_paths = []
        for raw_path in _string_list(route.get("primary_files")):
            text = str(raw_path)
            if text.endswith("SKILL.md"):
                skill_paths.append(text)

        for skill_path in dict.fromkeys(skill_paths):
            skill_id = _skill_id_from_path(skill_path)
            safe_path = _safe_resolve_skill_path(skill_path, skill_root=root)
            item = by_skill.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "path": safe_path["path"],
                    "unsafe_path": safe_path["unsafe_path"],
                    "linked_domains": [],
                    "source": "knowledge_map.json",
                },
            )
            if domain not in item["linked_domains"]:
                item["linked_domains"].append(domain)

    if root.exists():
        for skill_dir in root.iterdir():
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            item = by_skill.setdefault(
                skill_dir.name,
                {
                    "skill_id": skill_dir.name,
                    "path": str(skill_file),
                    "linked_domains": [],
                    "source": "filesystem",
                },
            )
            item["filesystem_path"] = str(skill_file)

    result = {
        "ok": not bool(payload.get("load_error")),
        "skill_root": str(root),
        "skills": sorted(by_skill.values(), key=lambda item: item["skill_id"]),
    }
    if payload.get("load_error"):
        result["load_error"] = payload["load_error"]
        result["load_error_detail"] = payload.get("load_error_detail")
    return result


def audit_skill_registry(*, skill_root: Path | None = None) -> dict[str, Any]:
    registry = load_skill_registry(skill_root=skill_root)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    if registry.get("load_error"):
        warnings.append(f"knowledge_map_load_error: {registry['load_error']}")
    for skill in registry["skills"]:
        unsafe_path = str(skill.get("unsafe_path") or "")
        path = Path(str(skill.get("filesystem_path") or skill.get("path") or ""))
        exists = False if unsafe_path else path.exists()
        linked_domains = list(skill.get("linked_domains") or [])
        if unsafe_path:
            warnings.append(f"unsafe skill path: {skill['skill_id']}")
        if not exists:
            warnings.append(f"missing skill file: {skill['skill_id']}")
        if exists and not linked_domains and skill.get("source") == "knowledge_map.json":
            warnings.append(f"skill is not linked from knowledge_map.json: {skill['skill_id']}")
        item = dict(skill)
        item["exists"] = exists
        item["linked_domains"] = linked_domains
        items.append(item)
    return {
        "ok": not any(
            warning.startswith(("missing skill file", "knowledge_map_load_error", "unsafe skill path"))
            for warning in warnings
        ),
        "skill_root": registry["skill_root"],
        "items": items,
        "warnings": warnings,
    }
