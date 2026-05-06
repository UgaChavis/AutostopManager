from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


SKILL_ROOT = Path("C:/Users/User/.codex/skills")
KNOWLEDGE_MAP_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_map.json"


def _load_knowledge_map() -> dict[str, Any]:
    if not KNOWLEDGE_MAP_PATH.exists():
        return {"domains": {}}
    return json.loads(KNOWLEDGE_MAP_PATH.read_text(encoding="utf-8-sig"))


def _skill_id_from_path(path: str) -> str:
    return Path(path).parent.name if path.endswith("SKILL.md") else Path(path).name


def load_skill_registry(*, skill_root: Path | None = None) -> dict[str, Any]:
    root = skill_root or SKILL_ROOT
    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    by_skill: dict[str, dict[str, Any]] = {}

    for domain, route in domains.items():
        raw_skill_path = str(route.get("skill_path") or "")
        skill_paths = []
        if raw_skill_path:
            skill_paths.append(raw_skill_path)
        for raw_path in route.get("source_of_truth_files", []):
            text = str(raw_path)
            if text.endswith("SKILL.md"):
                skill_paths.append(text)
        for raw_path in route.get("primary_files", []):
            text = str(raw_path)
            if text.endswith("SKILL.md"):
                skill_paths.append(text)

        for skill_path in dict.fromkeys(skill_paths):
            skill_id = _skill_id_from_path(skill_path)
            item = by_skill.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "path": skill_path,
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

    return {"ok": True, "skill_root": str(root), "skills": sorted(by_skill.values(), key=lambda item: item["skill_id"])}


def audit_skill_registry(*, skill_root: Path | None = None) -> dict[str, Any]:
    registry = load_skill_registry(skill_root=skill_root)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for skill in registry["skills"]:
        path = Path(str(skill.get("path") or ""))
        exists = path.exists()
        linked_domains = list(skill.get("linked_domains") or [])
        if not exists:
            warnings.append(f"missing skill file: {skill['skill_id']}")
        if exists and not linked_domains and skill["skill_id"].startswith(("bmw-", "toyota-", "autostop-")):
            warnings.append(f"skill is not linked from knowledge_map.json: {skill['skill_id']}")
        item = dict(skill)
        item["exists"] = exists
        item["linked_domains"] = linked_domains
        items.append(item)
    return {
        "ok": not any(warning.startswith("missing skill file") for warning in warnings),
        "skill_root": registry["skill_root"],
        "items": items,
        "warnings": warnings,
    }
