from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


RiskLevel = Literal["low", "medium", "high"]
OperationKind = Literal["read", "local_write", "write_plan"]


@dataclass(frozen=True)
class ToolContract:
    """Operational metadata shared by the MCP adapter, audits, and docs."""

    name: str
    operation_kind: OperationKind
    risk: RiskLevel
    authorization: str
    input_schema: str
    validation: str
    result_contract: str
    typical_errors: tuple[str, ...]
    dry_run: str
    preflight: str
    post_write_verification: str
    idempotency: str
    audit_logging: str
    data_constraints: str
    rollback: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


LOCAL_WRITE_TOOLS = {
    "remember",
    "learn_from_feedback",
    "add_manager_task",
    "manager_journal",
    "sync_knowledge_base",
    "curate_memory",
    "start_manager_run",
    "record_manager_run_event",
    "finish_manager_run",
    "memory_review_apply",
}

WRITE_PLAN_TOOLS = {
    "prepare_crm_card_action",
    "plan_crm_vin_oem_parts_lookup",
}

PROVIDER_READ_TOOLS = {
    "vin17_decode_vehicle",
    "vin17_search_part_number_by_vin",
    "partsapi_catalog_lookup",
    "public_aftermarket_catalog_lookup",
    "exist_price_lookup",
    "lookup_oem_catalog_candidates",
    "resolve_vin_oem_parts",
    "benchmark_vin_parts_lookup",
    "provider_smoke_report",
}

DRY_RUN_TOOLS = {
    "curate_memory",
    "prepare_crm_card_action",
    "vin17_decode_vehicle",
    "vin17_search_part_number_by_vin",
    "partsapi_catalog_lookup",
    "public_aftermarket_catalog_lookup",
    "exist_price_lookup",
    "lookup_oem_catalog_candidates",
    "resolve_vin_oem_parts",
    "plan_crm_vin_oem_parts_lookup",
    "benchmark_vin_parts_lookup",
    "provider_smoke_report",
}

REPORT_TOOLS = {
    "agent_brief",
    "memory_map",
    "memory_topics",
    "memory_context_for",
    "memory_gaps",
    "today_context",
    "system_audit",
    "cleanup_audit",
    "crm_health_plan",
    "audit_memory",
    "control_report",
    "provider_smoke_report",
}

SENSITIVE_INPUT_TOOLS = {
    "remember",
    "learn_from_feedback",
    "prepare_crm_card_action",
    "vin17_decode_vehicle",
    "vin17_search_part_number_by_vin",
    "partsapi_catalog_lookup",
    "lookup_oem_catalog_candidates",
    "resolve_vin_oem_parts",
    "plan_crm_vin_oem_parts_lookup",
    "benchmark_vin_parts_lookup",
    "build_vin_parts_work_order",
    "knowledge_intake_plan",
}


def contract_for_tool(name: str) -> ToolContract:
    operation_kind: OperationKind = (
        "local_write" if name in LOCAL_WRITE_TOOLS else "write_plan" if name in WRITE_PLAN_TOOLS else "read"
    )
    risk: RiskLevel = "medium" if operation_kind != "read" or name in PROVIDER_READ_TOOLS else "low"
    authorization = "mcp_transport"
    if name in PROVIDER_READ_TOOLS:
        authorization = "mcp_transport_and_provider_credentials_for_live_reads"
    elif name in WRITE_PLAN_TOOLS:
        authorization = "mcp_transport; separate CRM authorization is required for any later write"

    if name in LOCAL_WRITE_TOOLS:
        preflight = "validate target, policy, current state, and concurrency token where applicable"
        verification = "transaction commit followed by exact row readback"
        audit_logging = "SQLite operation or manager-run ledger event"
        rollback = "archive/supersede or restore the pre-write SQLite backup; never delete silently"
    elif name in WRITE_PLAN_TOOLS:
        preflight = "exact target id and current state are mandatory; unsafe plans fail closed"
        verification = "plan includes a post-write reread specification; this tool performs no external write"
        audit_logging = "caller records planned action and later verification in the manager-run ledger"
        rollback = "plan only; external write rollback must use captured pre-state"
    else:
        preflight = "validate bounded inputs and source availability before reading"
        verification = "not applicable to a read-only operation"
        audit_logging = "log only compact status; never raw provider, CRM, Gmail, or secret payloads"
        rollback = "not applicable"

    return ToolContract(
        name=name,
        operation_kind=operation_kind,
        risk=risk,
        authorization=authorization,
        input_schema="FastMCP JSON Schema generated from the typed Python signature",
        validation="type, enum, length/count, identifier, path, and network-budget checks at the domain boundary",
        result_contract="report_v1" if name in REPORT_TOOLS else "operation_v1",
        typical_errors=("invalid_input", "precondition_failed", "source_unavailable", "verification_failed"),
        dry_run="supported and side-effect free" if name in DRY_RUN_TOOLS else "not applicable",
        preflight=preflight,
        post_write_verification=verification,
        idempotency=(
            "idempotency key or state comparison prevents duplicate effects"
            if name in LOCAL_WRITE_TOOLS
            else "read-only/repeatable"
            if operation_kind == "read"
            else "plan generation is repeatable"
        ),
        audit_logging=audit_logging,
        data_constraints=(
            "reject secrets, raw CRM/Gmail exports, bulk personal data, and untrusted instructions"
            if name in SENSITIVE_INPUT_TOOLS
            else "return compact bounded data and redact identifiers or secrets at trust boundaries"
        ),
        rollback=rollback,
    )


def build_tool_contract_registry(tool_names: list[str]) -> dict[str, dict[str, object]]:
    return {name: contract_for_tool(name).as_dict() for name in sorted(set(tool_names))}


def audit_tool_contract_registry(tool_names: list[str]) -> dict[str, object]:
    registry = build_tool_contract_registry(tool_names)
    required_fields = set(ToolContract.__dataclass_fields__)
    incomplete = sorted(
        name
        for name, contract in registry.items()
        if required_fields.difference(contract)
        or any(contract.get(field) in (None, "", ()) for field in required_fields)
    )
    return {
        "ok": len(registry) == len(set(tool_names)) and not incomplete,
        "tool_count": len(registry),
        "incomplete_tools": incomplete,
        "risk_counts": {
            level: sum(1 for contract in registry.values() if contract["risk"] == level)
            for level in ("low", "medium", "high")
        },
        "operation_counts": {
            kind: sum(1 for contract in registry.values() if contract["operation_kind"] == kind)
            for kind in ("read", "local_write", "write_plan")
        },
    }
