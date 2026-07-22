from __future__ import annotations

import pytest

from autostop_manager.agent_case_resolver import agent_case_resolver
from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore


NOW = "2026-07-22T12:00:00+00:00"


class _FakeServer:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, name: str, description: str = "", **_kwargs):
        del description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_agent_case_resolver_builds_a_read_only_dependency_plan() -> None:
    result = agent_case_resolver(
        "plan",
        case_id="case_oem_001",
        claims=[
            {
                "claim_id": "identity",
                "subject_ref": "card:abc123",
                "predicate": "vehicle_identity",
                "required_source_kinds": ["crm"],
            },
            {
                "claim_id": "oem_part",
                "subject_ref": "card:abc123",
                "predicate": "oem_part_number",
                "risk": "high",
                "required_source_kinds": ["oem"],
                "depends_on": ["identity"],
            },
        ],
        sources=[
            {"source_id": "crm_context", "kind": "crm"},
            {"source_id": "parts_catalog", "kind": "oem"},
        ],
    )

    assert result["ok"] is True
    assert result["writes"] == []
    batches = result["plan"]["batches"]
    assert batches[0][0]["claim_id"] == "identity"
    assert batches[1][0]["claim_id"] == "oem_part"
    assert batches[1][0]["depends_on"] == ["case_oem_001:identity:crm_context:p0"]
    assert all(step["read_only"] for batch in batches for step in batch)


def test_agent_case_resolver_reconciles_scalar_evidence_with_display_only_value() -> None:
    result = agent_case_resolver(
        "reconcile",
        case_id="case_oem_002",
        claims=[
            {
                "claim_id": "oem_part",
                "subject_ref": "card:abc123",
                "predicate": "oem_part_number",
                "risk": "high",
                "required_source_kinds": ["oem"],
            }
        ],
        sources=[{"source_id": "parts_catalog", "kind": "oem"}],
        evidence=[
            {
                "evidence_id": "epc_1",
                "claim_id": "oem_part",
                "source_id": "parts_catalog",
                "source_kind": "oem",
                "value": "A1678350400",
                "observed_at": NOW,
            }
        ],
    )

    assert result["ok"] is True
    claim = result["resolution"]["claims"][0]
    assert claim["status"] == "resolved"
    assert claim["display_value"] == "A1678350400"
    assert "value" not in claim


def test_agent_case_resolver_rejects_raw_prompt_or_payload_fields() -> None:
    result = agent_case_resolver(
        "plan",
        case_id="case_invalid",
        claims=[
            {
                "claim_id": "identity",
                "subject_ref": "card:abc123",
                "predicate": "vehicle_identity",
                "prompt": "raw owner request must not enter the resolver DTO",
            }
        ],
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_agent_case_resolver_input"
    assert "raw owner request" not in str(result)


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Иван Петров",
        "ivan.petrov@example.com",
        "+7 999 123-45-67",
        "79991234567",
        "А123ВС124",
        "Иван Петров A1678350400",
    ],
)
def test_agent_case_resolver_rejects_personal_evidence_values_without_echoing_them(unsafe_value: str) -> None:
    result = agent_case_resolver(
        "reconcile",
        case_id="case_private_value",
        claims=[
            {
                "claim_id": "oem_part",
                "subject_ref": "card:abc123",
                "predicate": "oem_part_number",
                "required_source_kinds": ["oem"],
            }
        ],
        sources=[{"source_id": "parts_catalog", "kind": "oem"}],
        evidence=[
            {
                "evidence_id": "unsafe_1",
                "claim_id": "oem_part",
                "source_id": "parts_catalog",
                "source_kind": "oem",
                "value": unsafe_value,
                "observed_at": NOW,
            }
        ],
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_agent_case_resolver_input"
    assert unsafe_value not in str(result)


@pytest.mark.parametrize("technical_value", ["A1678350400", "11428570590", "MB 229.52", "replace_belt", "замена_ремня"])
def test_agent_case_resolver_keeps_compact_technical_evidence_values(technical_value: str) -> None:
    result = agent_case_resolver(
        "reconcile",
        case_id="case_technical_value",
        claims=[
            {
                "claim_id": "technical_fact",
                "subject_ref": "card:abc123",
                "predicate": "technical_fact",
                "required_source_kinds": ["oem"],
            }
        ],
        sources=[{"source_id": "parts_catalog", "kind": "oem"}],
        evidence=[
            {
                "evidence_id": "technical_1",
                "claim_id": "technical_fact",
                "source_id": "parts_catalog",
                "source_kind": "oem",
                "value": technical_value,
                "observed_at": NOW,
            }
        ],
    )

    assert result["ok"] is True
    assert result["resolution"]["claims"][0]["display_value"] == technical_value


def test_agent_case_resolver_is_registered_as_a_read_only_manager_tool(tmp_path) -> None:
    server = _FakeServer()
    register_manager_memory_tools(server, ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    assert "agent_case_resolver" in server.tools
    result = server.tools["agent_case_resolver"](
        "plan",
        "case_tool_001",
        [
            {
                "claim_id": "identity",
                "subject_ref": "card:abc123",
                "predicate": "vehicle_identity",
                "required_source_kinds": ["crm"],
            }
        ],
        sources=[{"source_id": "crm_context", "kind": "crm"}],
    )

    assert result["ok"] is True
    assert result["writes"] == []
