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
RELEASE_GATES_SCRIPT_PATH = ROOT / "scripts" / "release-gates.sh"

_MAP_FIELDS = {"title", "primary_files", "reference_files", "optional_runtime_files"}
_ROUTE_FIELDS = {
    "aliases",
    "command_id",
    "workflow_id",
    "intent",
    "priority",
    "knowledge_domains",
    "signals",
}
_SIGNAL_FIELDS = {"phrases", "all", "any", "exclude"}


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


def test_remote_access_playbook_keeps_remote_targets_separate():
    text = (ROOT / "docs/agent/codex_home_pc_reverse_ssh.md").read_text(encoding="utf-8")

    assert "/opt/autostop-managed-pc/README.md" in text
    assert "FST.KZ" in text and "AGENTS.md" in text
    assert "never reuse home-PC credentials" in text


def test_skills_have_metadata_and_one_knowledge_map_entrypoint():
    knowledge_map = _payload(KNOWLEDGE_MAP_PATH)
    domains = knowledge_map["domains"]
    assert isinstance(domains, dict) and domains

    declared_paths = {
        path
        for definition in domains.values()
        for path in _string_list(definition.get("primary_files"))
        if path.endswith("SKILL.md")
    }

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


def test_command_routes_are_compact_effect_free_suggestions():
    knowledge_map = _payload(KNOWLEDGE_MAP_PATH)
    known_domains = set(knowledge_map["domains"])
    payload = _payload(COMMAND_ROUTES_PATH)
    assert re.fullmatch(r"agent_command_registry_v\d+", str(payload["format"]))
    routes = payload["routes"]
    assert isinstance(routes, list)
    assert 8 <= len(routes) <= 12
    assert COMMAND_ROUTES_PATH.stat().st_size <= 8 * 1024

    command_ids: set[str] = set()
    claimed_intents: set[str] = set()
    for route in routes:
        assert isinstance(route, dict)
        assert set(route) <= _ROUTE_FIELDS
        for field in ("command_id", "workflow_id", "intent"):
            assert isinstance(route.get(field), str) and route[field].strip()
        assert route["command_id"] not in command_ids
        command_ids.add(route["command_id"])
        assert isinstance(route.get("priority"), int)
        domains = _string_list(route.get("knowledge_domains"))
        assert set(domains) <= known_domains
        aliases = _string_list(route.get("aliases", []))
        intents = [route["intent"], *aliases]
        assert claimed_intents.isdisjoint(intents)
        claimed_intents.update(intents)

        signals = route.get("signals")
        assert isinstance(signals, dict)
        assert set(signals) <= _SIGNAL_FIELDS
        for field in ("phrases", "any", "exclude"):
            _string_list(signals.get(field, []))
        all_groups = signals.get("all", [])
        assert isinstance(all_groups, list)
        for group in all_groups:
            _string_list(group)

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
    gates = RELEASE_GATES_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "./scripts/release-gates.sh" in text
    assert 'test -z "$(git status --porcelain=v1 --untracked-files=all)"' in text
    assert "run_manager_release_gates" not in text
    assert "store-conductor-release-gate" in text
    assert "AUTOSTOP_MANAGER_DB=/opt/AutostopManager/data/autostop_manager.sqlite3" in text
    assert "git fetch origin AutostopManager --prune" in text
    assert "git ls-remote origin refs/heads/AutostopManager" in text
    for command in (
        "knowledge-sync",
        "knowledge-audit",
        "skills-audit",
        "cleanup-audit",
        "ruff check",
        "mypy autostop_manager",
        "coverage run",
        "git diff --check",
    ):
        assert command in gates
