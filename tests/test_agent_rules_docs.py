from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS_PATH = ROOT / "AGENTS.md"
SKILL_ROOT = ROOT / ".agents" / "skills"
KNOWLEDGE_MAP_PATH = ROOT / "docs" / "agent" / "knowledge_map.json"
COMMAND_ROUTES_PATH = ROOT / "docs" / "agent" / "command_routes.json"
MANAGER_RULES_PATH = ROOT / "docs" / "agent" / "manager_rules.json"
DEPLOYMENT_RUNBOOK_PATH = ROOT / "docs" / "agent" / "deployment_runbook.md"

_MAP_FIELDS = {"title", "primary_files", "reference_files", "optional_runtime_files", "skill_path"}
_ROUTE_FIELDS = {
    "command_id",
    "workflow_id",
    "intent",
    "priority",
    "phase",
    "knowledge_domains",
    "effects",
    "dependencies",
    "signals",
}
_SIGNAL_FIELDS = {"phrases", "all", "any", "exclude", "action"}
_EFFECTS = {
    "account_auth",
    "crm_write",
    "destructive",
    "document",
    "external_send",
    "finance",
    "remote_diagnostics",
    "store_write",
}


def _payload(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _skill_paths() -> list[Path]:
    return sorted(SKILL_ROOT.glob("*/SKILL.md"))


def _relative_path(value: str) -> Path:
    path = Path(value)
    assert value and not path.is_absolute()
    assert ".." not in path.parts
    return ROOT / path


def _string_list(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) and item.strip() for item in value)
    assert len(value) == len(set(value))
    return value


def test_agent_startup_contract_is_small_and_has_no_legacy_entrypoint():
    assert AGENTS_PATH.is_file()
    assert AGENTS_PATH.stat().st_size <= 32 * 1024
    assert not (ROOT / "agent.md").exists()


def test_skills_have_metadata_and_one_knowledge_map_entrypoint():
    knowledge_map = _payload(KNOWLEDGE_MAP_PATH)
    domains = knowledge_map["domains"]
    assert isinstance(domains, dict) and domains

    declared_paths: set[str] = set()
    for definition in domains.values():
        if "skill_path" in definition:
            path = definition["skill_path"]
            assert isinstance(path, str)
            declared_paths.add(path)

    skills = _skill_paths()
    assert skills
    for path in skills:
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        metadata = re.match(r"\A---\n([\s\S]*?)\n---\n", text)
        assert metadata is not None
        assert re.search(r"^name:\s*\S", metadata.group(1), flags=re.MULTILINE)
        assert re.search(r"^description:\s*\S", metadata.group(1), flags=re.MULTILINE)
        assert relative in declared_paths


def test_knowledge_map_has_safe_resolvable_owners():
    knowledge_map = _payload(KNOWLEDGE_MAP_PATH)
    assert re.fullmatch(r"knowledge_navigation_v\d+", str(knowledge_map["format"]))
    assert knowledge_map["entrypoint"] == "AGENTS.md"
    domains = knowledge_map["domains"]
    assert isinstance(domains, dict) and domains

    for domain, definition in domains.items():
        assert isinstance(domain, str) and domain
        assert isinstance(definition, dict)
        assert set(definition) <= _MAP_FIELDS
        assert isinstance(definition.get("title"), str) and definition["title"].strip()
        primary = _string_list(definition.get("primary_files"))
        assert primary
        for field in ("primary_files", "reference_files"):
            for raw_path in _string_list(definition.get(field, [])):
                assert _relative_path(raw_path).is_file()
        for raw_path in _string_list(definition.get("optional_runtime_files", [])):
            _relative_path(raw_path)
        skill_path = definition.get("skill_path")
        if skill_path is not None:
            assert isinstance(skill_path, str)
            assert skill_path in primary
            assert _relative_path(skill_path).is_file()


def test_command_routes_are_structural_and_keep_effects_explicit():
    knowledge_map = _payload(KNOWLEDGE_MAP_PATH)
    known_domains = set(knowledge_map["domains"])
    payload = _payload(COMMAND_ROUTES_PATH)
    assert re.fullmatch(r"agent_command_registry_v\d+", str(payload["format"]))
    routes = payload["routes"]
    assert isinstance(routes, list) and routes

    command_ids: set[str] = set()
    for route in routes:
        assert isinstance(route, dict)
        assert set(route) <= _ROUTE_FIELDS
        for field in ("command_id", "workflow_id", "intent"):
            assert isinstance(route.get(field), str) and route[field].strip()
        assert route["command_id"] not in command_ids
        command_ids.add(route["command_id"])
        assert isinstance(route.get("priority"), int)
        assert isinstance(route.get("phase"), int)
        domains = _string_list(route.get("knowledge_domains"))
        assert set(domains) <= known_domains
        effects = _string_list(route.get("effects"))
        assert set(effects) <= _EFFECTS
        _string_list(route.get("dependencies", []))

        signals = route.get("signals")
        assert isinstance(signals, dict)
        assert set(signals) <= _SIGNAL_FIELDS
        for field in ("phrases", "any", "exclude", "action"):
            _string_list(signals.get(field, []))
        all_groups = signals.get("all", [])
        assert isinstance(all_groups, list)
        for group in all_groups:
            _string_list(group)

        if "intake" in route["intent"]:
            assert effects == []

    assert any("store_management" in route["knowledge_domains"] for route in routes)
    assert any("service_case" in route["knowledge_domains"] for route in routes)


def test_manager_rules_are_ordered_cross_system_safety_invariants():
    payload = _payload(MANAGER_RULES_PATH)
    assert re.fullmatch(r"manager_runtime_invariants_v\d+", str(payload["format"]))
    rules = payload["rules"]
    assert isinstance(rules, list) and rules

    ids: set[str] = set()
    priorities: list[int] = []
    for rule in rules:
        assert set(rule) == {"id", "priority", "rule"}
        assert isinstance(rule["id"], str) and rule["id"]
        assert rule["id"] not in ids
        ids.add(rule["id"])
        assert isinstance(rule["priority"], int)
        priorities.append(rule["priority"])
        assert isinstance(rule["rule"], str) and rule["rule"].strip()

    assert priorities == sorted(priorities)
    assert {
        "source-and-privacy",
        "route-and-authority",
        "workflow-and-release",
    } <= ids


def test_instruction_surface_is_readable_and_has_no_inline_secret_assignment():
    paths = [AGENTS_PATH, COMMAND_ROUTES_PATH, KNOWLEDGE_MAP_PATH, MANAGER_RULES_PATH, *_skill_paths()]
    secret_assignment = re.compile(
        r"(?i)(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*(?!<|\$|your|xxx|\*)\S+"
    )

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert secret_assignment.search(text) is None
        if path.suffix == ".json":
            assert isinstance(json.loads(text), dict)
        else:
            assert sum(line.lstrip().startswith("```") for line in text.splitlines()) % 2 == 0


def test_deployment_runbook_checks_persistent_store_conductor_state_before_release():
    text = DEPLOYMENT_RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "store-conductor-release-gate" in text
    assert "AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3" in text
    assert "git fetch origin AutostopManager --prune" in text
    assert "git ls-remote origin refs/heads/AutostopManager" in text
