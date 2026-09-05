from __future__ import annotations

import hashlib
from collections.abc import Collection
from typing import Any

from mcp.types import ToolAnnotations

from .action_contract import prepare_action_contract
from .agent_case_resolver import agent_case_resolver
from .agent_gateway import agent_envelope, build_agent_bootstrap, list_agent_workflows
from .catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from .catalog_clients import (
    exist_price_lookup,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    vin17_decode_vehicle,
    vin17_search_part_number_by_vin,
)
from .cleanup_audit import build_cleanup_audit
from .config import (
    get_store_api_url,
    get_store_manage_token,
    get_store_owner_token,
    get_store_quote_token,
    get_store_read_token,
)
from .control_center import build_control_report, format_control_report_markdown
from .context import build_agent_brief, prepare_manager_context
from .knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from .memory_curator import audit_memory, curate_memory
from .partsapi_category_index import (
    explain_partsapi_category_for_intent,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)
from .public_automotive_evidence import lookup_public_automotive_evidence
from .skill_registry import audit_skill_registry
from .source_catalog import recommend_automotive_sources
from .storage import ManagerMemoryStore
from .store_api import StoreApiClient
from .store_analytics import get_store_analytics_report
from .store_integration import StoreIntegration
from .store_owner_api import StoreOwnerApiClient
from .store_quote_conductor import StoreQuoteConductor, StoreQuoteOwnerApi
from .system_audit import build_system_audit
from .vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from .vin_parts_benchmark import benchmark_vin_parts_lookup
from .vin_oem_resolver import resolve_vin_oem_parts
from .vin_lookup import lookup_original_parts
from .work_pricing import estimate_repair_work_cost


def _registered_tools(server: Any) -> dict[str, Any] | None:
    tools = getattr(server, "tools", None)
    if not isinstance(tools, dict):
        tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    return tools if isinstance(tools, dict) else None


def _registered_tool_names(server: Any) -> list[str] | None:
    tools = _registered_tools(server)
    return sorted(str(name) for name in tools) if tools is not None else None


def _registered_tool_schemas(server: Any) -> dict[str, Any] | None:
    tools = _registered_tools(server)
    schemas = {
        str(name): tool.parameters
        for name, tool in (tools or {}).items()
        if isinstance(getattr(tool, "parameters", None), dict)
    }
    return schemas if tools and len(schemas) == len(tools) else None


def _workflow_envelope(result: dict[str, Any], *, next_actions: list[str] | None = None) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    run_id = result.get("run_id") or result.get("id")
    warnings = [] if ok else [str(result.get("error") or "workflow_operation_failed")]
    return agent_envelope(
        ok=ok,
        status=str(result.get("status") or ("failed" if not ok else "completed")),
        run_id=int(run_id) if isinstance(run_id, int) else None,
        summary=result,
        warnings=warnings,
        next_actions=next_actions or [],
    )


# Registration is intentionally declarative; each nested tool delegates to tested domain functions or storage methods.
def register_manager_memory_tools(  # noqa: C901
    server: Any,
    store: ManagerMemoryStore | None = None,
    store_client: StoreApiClient | None = None,
    include_tools: Collection[str] | None = None,
) -> None:
    original_tool = server.tool
    if include_tools is not None:
        selected_tools = frozenset(include_tools)

        def filtered_tool(*args: Any, **kwargs: Any) -> Any:
            if str(kwargs.get("name") or "") in selected_tools:
                return original_tool(*args, **kwargs)
            return lambda function: function

        server.tool = filtered_tool
    memory = store or ManagerMemoryStore()
    store_adapter = StoreIntegration(
        client=store_client
        or StoreApiClient(
            api_url=get_store_api_url(),
            read_token=get_store_read_token(),
            manage_token=get_store_manage_token(),
            quote_token=get_store_quote_token(),
        ),
        store=memory,
    )
    store_owner_client = StoreOwnerApiClient(
        agent_api_url=get_store_api_url(),
        owner_token=get_store_owner_token(),
    )
    quote_conductor = StoreQuoteConductor(
        store=memory,
        gateway=StoreQuoteOwnerApi(store_owner_client),
    )

    @server.tool(
        name="remember",
        description=(
            "Store long-term manager memory that does not belong in AutoStop CRM cards: "
            "facts, agreements, personal matters, rent notes, operating context, durable conclusions from approved source files, or useful experience."
        ),
    )
    def remember(
        content: str,
        kind: str = "note",
        title: str = "",
        category: str = "general",
        source: str = "chatgpt",
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        expires_at: str | None = None,
        supersedes_id: int | None = None,
        sensitivity: str = "normal",
    ) -> dict[str, Any]:
        return memory.remember(
            content,
            kind="fact" if kind == "fact" else "note",
            title=title,
            category=category,
            source=source,
            tags=tags,
            importance=importance,
            confidence=confidence,
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            sensitivity=sensitivity,
        )

    @server.tool(
        name="recall",
        description=(
            "Search the manager long-term memory with relevance scoring and optional kind/category/tag filters. "
            "Use this before assuming owner context, style preferences, operating lessons, or durable rules are unknown."
        ),
    )
    def recall(
        query: str = "",
        limit: int = 20,
        kind: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.recall(query, limit=limit, kind=kind, category=category, tags=tags)

    @server.tool(
        name="learn_from_feedback",
        description=(
            "Store a concise reusable lesson when owner feedback, praise, criticism, clear success, or clear failure "
            "should improve future manager behavior. Store the lesson, not CRM/Gmail/raw event copies."
        ),
    )
    def learn_from_feedback(
        content: str,
        title: str = "",
        applies_to: str = "general",
        signal: str = "manager_observation",
        recommendation: str = "",
        avoid: str = "",
        importance: float = 0.5,
        confidence: float = 0.7,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.learn_from_feedback(
            content,
            title=title,
            applies_to=applies_to,
            signal=signal,
            recommendation=recommendation,
            avoid=avoid,
            importance=importance,
            confidence=confidence,
            source=source,
            tags=tags,
        )

    @server.tool(
        name="recall_lessons",
        description="Search reusable manager lessons by task text, applies_to, signal, and tags before similar work.",
    )
    def recall_lessons(
        query: str = "",
        limit: int = 20,
        applies_to: str | None = None,
        signal: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.recall_lessons(query, limit=limit, applies_to=applies_to, signal=signal, tags=tags)

    @server.tool(
        name="memory_map",
        description="Return compact manager-memory section counts and timestamps.",
    )
    def memory_map() -> dict[str, Any]:
        return memory.memory_map()

    @server.tool(
        name="memory_topics",
        description="Return memory categories and tags with counts and examples for navigation and review.",
    )
    def memory_topics(examples_limit: int = 3) -> dict[str, Any]:
        return memory.memory_topics(examples_limit=examples_limit)

    @server.tool(
        name="memory_context_for",
        description="Return task-relevant lessons, preferences or facts, and source boundaries.",
    )
    def memory_context_for(task: str, limit: int = 5) -> dict[str, Any]:
        return memory.memory_context_for(task, limit=limit)

    @server.tool(
        name="memory_gaps",
        description="Return sparse and empty manager-memory sections plus detected conflicts.",
    )
    def memory_gaps() -> dict[str, Any]:
        return memory.memory_gaps()

    @server.tool(
        name="add_manager_task",
        description="Add a manager-level task that is not a CRM vehicle card or repair order.",
    )
    def add_manager_task(
        title: str,
        details: str = "",
        due_at: str | None = None,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.add_task(title, details=details, due_at=due_at, source=source, tags=tags)

    @server.tool(
        name="today_context",
        description="Return due manager tasks, recent journal entries, rules and warnings.",
    )
    def today_context(limit: int = 20) -> dict[str, Any]:
        return memory.today_context(limit=limit)

    @server.tool(
        name="prepare_manager_context",
        description="Suggest compact workflow candidates and source pointers for a request.",
    )
    def prepare_manager_context_tool(
        query: str,
        intent: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return prepare_manager_context(memory, query, intent=intent, limit=limit)

    @server.tool(
        name="agent_brief",
        description=(
            "Return a compact outcome-driven starting brief: recommended workflow, source pointers, effects "
            "and verification goals. Routes are guidance, not a script."
        ),
    )
    def agent_brief_tool(
        query: str,
        intent: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        return build_agent_brief(memory, query, intent=intent, limit=limit)

    @server.tool(
        name="agent_bootstrap",
        description=(
            "Return an adaptive Agent Gateway v2 starting point with suggested workflows, source boundaries "
            "and resumable unfinished runs. It does not access live business systems."
        ),
    )
    def agent_bootstrap_tool(
        query: str = "",
        intent: str | None = None,
        limit: int = 8,
        mode_override: str | None = None,
        external_turn_id: str = "",
    ) -> dict[str, Any]:
        return build_agent_bootstrap(
            memory,
            query=query,
            intent=intent,
            limit=limit,
            mode_override=mode_override,
            external_turn_id=external_turn_id,
        )

    @server.tool(
        name="agent_mode",
        description=(
            "Read, set, or resolve the durable AgentExecutionMode. `work` keeps the normal fast workflow; "
            "`learning` requires a technical post-run review. Per-turn overrides are resolved without storing prompt text."
        ),
    )
    def agent_mode_tool(
        action: str = "get",
        mode: str = "",
        mode_override: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        normalized_action = str(action or "get").strip().casefold()
        if normalized_action == "get":
            return memory.get_agent_mode()
        if normalized_action == "set":
            return memory.set_agent_mode(mode, expected_state_version=expected_state_version)
        if normalized_action == "resolve":
            return memory.resolve_agent_mode(mode_override)
        return {"ok": False, "error": "invalid_agent_mode_action", "supported_actions": ["get", "set", "resolve"]}

    @server.tool(
        name="post_run_review",
        description=(
            "Close one learning-mode turn with an ExperienceReviewV1. Accept only completion codes, tool health, "
            "safe technical metadata, and an optional improvement category; raw prompts, CRM/Store/Gmail payloads, "
            "VINs, contacts, and financial values are rejected."
        ),
    )
    def post_run_review_tool(
        turn_id: str,
        outcome: str = "confirmed",
        completion_checks: list[str] | None = None,
        tool_assessment: list[dict[str, Any]] | None = None,
        failure_class: str = "",
        improvement_kind: str = "",
        risk: str = "low",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return memory.post_run_review(
            turn_id,
            outcome=outcome,
            completion_checks=completion_checks,
            tool_assessment=tool_assessment,
            failure_class=failure_class,
            improvement_kind=improvement_kind,
            risk=risk,
            metadata=metadata,
        )

    @server.tool(
        name="agent_learning_workflow",
        description=(
            "Advance a reviewed learning improvement candidate through repair, verify, promote, defer, rollback, or summary. "
            "This records lifecycle evidence only; actual code/docs/deploy work remains subject to the normal task and verification flow."
        ),
    )
    def agent_learning_workflow_tool(
        operation: str,
        candidate_id: str = "",
        turn_id: str = "",
        verification: dict[str, Any] | None = None,
        reason_code: str = "",
        lesson_content: str = "",
        lesson_title: str = "",
        applies_to: str = "general",
    ) -> dict[str, Any]:
        return memory.agent_learning_workflow(
            operation,
            candidate_id=candidate_id,
            turn_id=turn_id,
            verification=verification,
            reason_code=reason_code,
            lesson_content=lesson_content,
            lesson_title=lesson_title,
            applies_to=applies_to,
        )

    server.tool(
        name="agent_case_resolver",
        description=(
            "READ_ONLY RAW_CAPABILITY: Build a connector-neutral Case Resolver read plan or reconcile compact scalar "
            "evidence for one opaque case. It never calls connectors or writes CRM, Store, Gmail, memory, a workflow "
            "ledger, or files. Prompt text, raw connector payloads, and personal identifiers are rejected; resolution "
            "returns redacted display values only."
        ),
        annotations=ToolAnnotations(
            title="Agent Case Resolver",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )(agent_case_resolver)

    server.tool(
        name="list_agent_workflows",
        description=(
            "List the compact named Codex workflow registry and resolve a query/intent without exposing raw CRM or Gmail data."
        ),
    )(list_agent_workflows)

    @server.tool(
        name="get_store_analytics_report",
        description=(
            "READ_ONLY RAW_CAPABILITY: Return one aggregate Store report for the requested period; "
            "never return raw events, identifiers, search text or customer data."
        ),
    )
    def get_store_analytics_report_tool(
        query: str = "",
        period: str = "auto",
        date_from: str | None = None,
        date_to: str | None = None,
        top_limit: int = 10,
    ) -> dict[str, Any]:
        return get_store_analytics_report(
            api_url=get_store_api_url(),
            read_token=get_store_read_token(),
            query=query,
            period=period,
            date_from=date_from,
            date_to=date_to,
            top_limit=top_limit,
        )

    @server.tool(
        name="store_owner_capabilities",
        description=(
            "READ_ONLY RAW_CAPABILITY: List owner-scoped Store OpenAPI operations or describe one "
            "validation-only input contract; never return business data."
        ),
        annotations=ToolAnnotations(
            title="Store Owner Capabilities",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def store_owner_capabilities_tool(
        query: str = "",
        limit: int = 200,
        operation_id: str = "",
    ) -> dict[str, Any]:
        return store_owner_client.list_capabilities(
            query=query,
            limit=limit,
            operation_id=operation_id,
        )

    @server.tool(
        name="store_owner_api",
        description=(
            "OWNER_SCOPED RAW_CAPABILITY: Invoke one typed Store operation. Writes require the exact "
            "target and revision, ActionContractV2, idempotency, dry-run proof and reread; results stay transient."
        ),
        annotations=ToolAnnotations(
            title="Store Owner API",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def store_owner_api_tool(
        operation_id: str,
        mode: str = "dry_run",
        target_id: str = "",
        path_parameters: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        owner_intent: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        expected_revision: str | None = None,
        expected_contract_id: str | None = None,
        prepare_for_mode: str = "dry_run",
        dry_run_proof: str | None = None,
        allow_binary_response: bool = False,
    ) -> dict[str, Any]:
        prepared = store_owner_client.prepare_invocation(
            operation_id=operation_id,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            expected_revision=expected_revision,
        )
        if not prepared.get("ok"):
            return prepared
        raw_capability = prepared.get("summary")
        capability: dict[str, Any] = raw_capability if isinstance(raw_capability, dict) else {}
        method = str(capability.get("method") or "").upper()
        normalized_mode = str(mode or "").strip().casefold()
        normalized_prepare_for_mode = str(prepare_for_mode or "").strip().casefold()
        if normalized_mode == "prepare" and normalized_prepare_for_mode not in {
            "dry_run",
            "apply",
        }:
            return {
                "ok": False,
                "format": "autostop_store_owner_api_v1",
                "status": "blocked",
                "error": {"code": "store_owner_prepare_mode_invalid"},
                "summary": {"operation_id": operation_id},
                "data_included": False,
            }
        normalized_target = str(target_id or "").strip()
        contract_id: str | None = None
        effective_correlation_id = str(correlation_id or "").strip()
        if method != "GET":
            parameter_names = capability.get("path_parameters")
            names = parameter_names if isinstance(parameter_names, list) else []
            supplied_path_parameters = path_parameters if isinstance(path_parameters, dict) else {}
            expected_target = ""
            if len(names) == 1:
                expected_target = str(supplied_path_parameters.get(str(names[0])) or "").strip()
            elif len(names) > 1:
                expected_target = f"path:{capability.get('concrete_path') or ''!s}"
            elif capability.get("revision_required") is False:
                expected_target = f"collection:{capability.get('path') or ''!s}"
            else:
                expected_target = f"path:{capability.get('concrete_path') or ''!s}"
            if expected_target and normalized_target != expected_target:
                return {
                    "ok": False,
                    "format": "autostop_store_owner_api_v1",
                    "status": "blocked",
                    "error": {"code": "store_owner_target_binding_mismatch"},
                    "summary": {"expected_target_ref": expected_target},
                    "data_included": False,
                }
            if normalized_mode != "revision" and not effective_correlation_id:
                return {
                    "ok": False,
                    "format": "autostop_store_owner_api_v1",
                    "status": "blocked",
                    "error": {"code": "store_owner_correlation_id_required"},
                    "summary": {"operation_id": operation_id},
                    "data_included": False,
                }
            if normalized_mode != "revision":
                contract = prepare_action_contract(
                    domain="store_owner_api",
                    action="execute_owner_api",
                    target_id=target_id,
                    planned_changes={
                        "operation_id": operation_id,
                        "method": method,
                        "path_template": str(capability.get("path") or ""),
                        "risk": str(capability.get("risk") or ""),
                        "schema_hash": str(capability.get("schema_hash") or ""),
                        "concrete_path": str(capability.get("concrete_path") or ""),
                        "query_fields": capability.get("query_fields") or [],
                        "query_sha256": str(capability.get("query_sha256") or ""),
                        "request_sha256": str(capability.get("request_sha256") or ""),
                        "plan_hash": str(capability.get("plan_hash") or ""),
                        "verification_class": str(capability.get("verification_class") or ""),
                        "body_fields": sorted(body) if isinstance(body, dict) else [],
                        "form_fields": sorted(form) if isinstance(form, dict) else [],
                        "file_fields": sorted(
                            {
                                str(item.get("field") or "")
                                for item in files or []
                                if isinstance(item, dict) and str(item.get("field") or "")
                            }
                        ),
                    },
                    owner_intent=owner_intent,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    dry_run=(
                        normalized_prepare_for_mode == "dry_run"
                        if normalized_mode == "prepare"
                        else normalized_mode != "apply"
                    ),
                )
                if not contract.get("ok") or not (
                    isinstance(contract.get("execution"), dict) and contract["execution"].get("ready")
                ):
                    return {
                        "ok": False,
                        "format": "autostop_store_owner_api_v1",
                        "status": "blocked",
                        "error": {"code": "store_owner_action_contract_blocked"},
                        "summary": {
                            "contract_id": contract.get("contract_id"),
                            "blocking_reasons": (
                                contract.get("preflight", {}).get("blocking_reasons", [])
                                if isinstance(contract.get("preflight"), dict)
                                else []
                            ),
                        },
                        "data_included": False,
                    }
                contract_id = str(contract.get("contract_id") or "") or None
                effective_correlation_id = str(contract.get("correlation_id") or "")
                if normalized_mode in {"dry_run", "apply"}:
                    normalized_expected_contract = str(expected_contract_id or "").strip()
                    if not normalized_expected_contract:
                        return {
                            "ok": False,
                            "format": "autostop_store_owner_api_v1",
                            "status": "blocked",
                            "error": {"code": "store_owner_expected_contract_id_required"},
                            "summary": {"operation_id": operation_id},
                            "data_included": False,
                        }
                    if normalized_expected_contract != contract_id:
                        return {
                            "ok": False,
                            "format": "autostop_store_owner_api_v1",
                            "status": "conflict",
                            "error": {"code": "store_owner_action_contract_mismatch"},
                            "summary": {"operation_id": operation_id},
                            "data_included": False,
                        }
        technical_meta = {
            "contract_id": contract_id,
            "operation_id": operation_id,
            "request_sha256": str(capability.get("request_sha256") or ""),
            "schema_hash": str(capability.get("schema_hash") or ""),
            "verification_class": str(capability.get("verification_class") or ""),
            "correlation_id": effective_correlation_id or None,
            "target_ref_sha256": (
                hashlib.sha256(f"target:{normalized_target}".encode()).hexdigest() if normalized_target else None
            ),
            "expected_revision_sha256": (
                hashlib.sha256(f"expected:{expected_revision}".encode()).hexdigest()
                if expected_revision is not None
                else None
            ),
        }
        if normalized_mode == "prepare":
            return {
                "ok": True,
                "format": "autostop_store_owner_api_v1",
                "status": "validated",
                "summary": {
                    "operation_id": operation_id,
                    "method": method,
                    "risk": str(capability.get("risk") or ""),
                    "prepared_for_mode": normalized_prepare_for_mode,
                    "request_dispatched": False,
                },
                "meta": {
                    **technical_meta,
                    "request_dispatched": False,
                    "domain_handler_executed": False,
                },
                "data_included": False,
            }
        result = store_owner_client.invoke(
            operation_id=operation_id,
            mode=mode,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            owner_intent=owner_intent,
            idempotency_key=idempotency_key,
            correlation_id=effective_correlation_id,
            expected_revision=expected_revision,
            dry_run_proof=dry_run_proof,
            allow_binary_response=allow_binary_response,
            expected_plan_hash=str(capability.get("plan_hash") or "") or None,
        )
        meta = result.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.update(technical_meta)
        return result

    @server.tool(
        name="store_runtime_status",
        description=("INTERNAL_ONLY: Return redacted Store adapter readiness and optional live health for Gateway v2."),
    )
    def store_runtime_status_tool(
        live: bool = False,
        bootstrap_snapshot: bool = False,
    ) -> dict[str, Any]:
        return store_adapter.runtime_status(
            live=live,
            bootstrap_snapshot=bootstrap_snapshot,
        )

    @server.tool(
        name="store_digest",
        description=(
            "INTERNAL_ONLY: Read one bounded Store digest page. Acknowledge its cursor before advancing; "
            "the first read creates a baseline and Manager persists no raw payload."
        ),
    )
    def store_digest_tool(
        baseline: bool = False,
        since: str | None = None,
        cursor: str | None = None,
        ack_token: str | None = None,
        limit: int = 25,
        stream: str = "store_digest",
    ) -> dict[str, Any]:
        return store_adapter.digest(
            baseline=baseline,
            since=since,
            cursor=cursor,
            ack_token=ack_token,
            limit=limit,
            stream=stream,
        )

    @server.tool(
        name="store_search",
        description=(
            "INTERNAL_ONLY: Search an allowlisted Store entity with bounded pagination and redacted contacts."
        ),
    )
    def store_search_tool(
        entity: str,
        query: str = "",
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        return store_adapter.search(entity=entity, query=query, filters=filters, cursor=cursor, limit=limit)

    @server.tool(
        name="store_entity_context",
        description=(
            "INTERNAL_ONLY: Read one exact Store entity. General reads are redacted; full quote data uses "
            "the scoped credential and remains transient."
        ),
    )
    def store_entity_context_tool(
        entity: str,
        entity_id: str,
        detail: str = "summary",
    ) -> dict[str, Any]:
        return store_adapter.entity_context(entity=entity, entity_id=entity_id, detail=detail)

    @server.tool(
        name="download_store_quote_vin_photo",
        description=("INTERNAL_ONLY: Read a bounded transient JPEG preview for one exact Store quote VIN photo."),
        annotations=ToolAnnotations(
            title="Store Quote VIN Photo Preview",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def download_store_quote_vin_photo_tool(
        quote_request_id: str,
        expected_photo_sha256: str,
    ) -> dict[str, Any]:
        return store_adapter.quote_vin_photo_preview(
            quote_request_id=quote_request_id,
            expected_photo_sha256=expected_photo_sha256,
        )

    @server.tool(
        name="store_management_action",
        description=(
            "INTERNAL_ONLY: Run one allowlisted Store management operation with ActionContractV2, exact "
            "preread, dry-run/apply, idempotency, optimistic concurrency and reread."
        ),
    )
    def store_management_action_tool(
        domain: str,
        action: str,
        target_id: str,
        planned_changes: dict[str, Any],
        owner_intent: str,
        expected_updated_at: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str = "dry_run",
    ) -> dict[str, Any]:
        return store_adapter.management_action(
            domain=domain,
            action=action,
            target_id=target_id,
            planned_changes=planned_changes,
            owner_intent=owner_intent,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
        )

    @server.tool(
        name="store_quote_conductor",
        description=(
            "INTERNAL_ONLY: Advance one Store quote through Admin V2; use the work Telegram workflow for dialogue. "
            "Supports start, status, evidence, draft, publish, reopen, order, handoff and decline; writes use the exact "
            "current quote and a confirmed reread."
        ),
    )
    def store_quote_conductor_tool(
        operation: str,
        quote_request_id: str = "",
        run_id: int | None = None,
        expected_state_version: int | None = None,
        expected_revision: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        entries: list[dict[str, Any]] | None = None,
        coverage: list[dict[str, Any]] | None = None,
        customer_response: str = "",
        evidence: dict[str, Any] | None = None,
        consent_context_hash: str = "",
        published_snapshot_hash: str = "",
        mode: str = "apply",
    ) -> dict[str, Any]:
        return quote_conductor.execute(
            operation=operation,
            quote_request_id=quote_request_id,
            run_id=run_id,
            expected_state_version=expected_state_version,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            entries=entries,
            coverage=coverage,
            customer_response=customer_response,
            evidence=evidence,
            consent_context_hash=consent_context_hash,
            published_snapshot_hash=published_snapshot_hash,
            mode=mode,
        )

    server.tool(
        name="prepare_action_contract",
        description=(
            "Build a connector-neutral ActionContractV2 for CRM, AutoStop App store, finance, inventory, documents, files, or Gmail writes. "
            "Requires task intent, exact target where applicable, idempotency, concurrency, automatic preflight, compensation, "
            "and readback verification; never performs the write."
        ),
    )(prepare_action_contract)

    @server.tool(
        name="manager_journal",
        description="Append a bounded generic manager event without copying CRM records, correspondence, private identifiers, or money data.",
    )
    def manager_journal(
        event: str = "",
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.journal(event, source=source, tags=tags)

    @server.tool(
        name="sync_knowledge_base",
        description=(
            "Index docs/agent/knowledge_map.json, routed playbooks, source catalogs, and model-specific skills into SQLite "
            "so the manager can navigate local knowledge without reading every file."
        ),
    )
    def sync_knowledge_base_tool() -> dict[str, Any]:
        return sync_knowledge_base(memory)

    @server.tool(
        name="probe_knowledge_base",
        description=(
            "Cheaply check whether the local knowledge base has relevant knowledge for a vehicle, brand, model, system, or task. "
            "Use this before broad search or full document reads; if has_knowledge is true, open the returned source_of_truth/open_first route first."
        ),
    )
    def probe_knowledge_base_tool(
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        return probe_knowledge_base(memory, query, limit=limit)

    @server.tool(
        name="search_knowledge_base",
        description=(
            "Search the indexed AutostopManager knowledge base by query and optional domain. "
            "Use before broad file reads for diagnostics, fluids, VIN/OEM, parts, CRM management, or model-specific knowledge."
        ),
    )
    def search_knowledge_base_tool(
        query: str,
        domain: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return search_knowledge_base(memory, query, domain=domain, limit=limit)

    @server.tool(
        name="audit_knowledge_base",
        description=(
            "Audit docs/agent/knowledge_map.json, mapped source files, and SQLite document index counts. "
            "Use after knowledge intake or when local knowledge routing looks stale."
        ),
    )
    def audit_knowledge_base_tool() -> dict[str, Any]:
        return audit_knowledge_base(memory)

    @server.tool(
        name="audit_skill_registry",
        description="Audit local Codex skills linked from AutostopManager knowledge routes.",
    )
    def audit_skill_registry_tool() -> dict[str, Any]:
        return audit_skill_registry()

    @server.tool(
        name="cleanup_audit",
        description="Run the dry-run cleanup audit for cache, duplicate, and knowledge cleanup candidates without deleting files.",
    )
    def cleanup_audit_tool() -> dict[str, Any]:
        return build_cleanup_audit(store=memory)

    @server.tool(
        name="system_audit",
        description="Run the canonical read-only AutoStop Manager health audit without running pytest or mutating CRM/files.",
    )
    def system_audit_tool() -> dict[str, Any]:
        return build_system_audit(
            store=memory,
            registered_tool_names=_registered_tool_names(server),
            registered_tool_schemas=_registered_tool_schemas(server),
        )

    @server.tool(
        name="control_report",
        description=(
            "Generate ControlReportV1: server/runtime/Codex readiness, system health, git state, tests/doctor route, "
            "memory/knowledge/MCP/provider readiness, production ops gates, public ports, risks, and last run ledger. "
            "Read-only and secrets-redacted."
        ),
    )
    def control_report_tool(format: str = "json") -> dict[str, Any]:
        report = build_control_report(store=memory)
        if format == "markdown":
            return {
                "ok": True,
                "format": "markdown",
                "markdown": format_control_report_markdown(report),
                "report": report,
            }
        return report

    @server.tool(
        name="audit_memory",
        description="Audit long-term manager memory for duplicate, expired, and superseded memories.",
    )
    def audit_memory_tool() -> dict[str, Any]:
        return audit_memory(memory)

    @server.tool(
        name="curate_memory",
        description="Non-destructively curate long-term memory. With apply=true, archive duplicate note/fact copies.",
    )
    def curate_memory_tool(apply: bool = False) -> dict[str, Any]:
        return curate_memory(memory, apply=apply)

    @server.tool(
        name="start_workflow",
        description=(
            "Start an idempotent Agent Gateway v2 workflow in planned state. This records compact scope/refs only and does not call CRM or Gmail."
        ),
    )
    def start_workflow_tool(
        workflow_id: str,
        intent: str,
        idempotency_key: str,
        query: str = "",
        request_id: str = "",
        correlation_id: str = "",
        actor: str = "codex-owner-agent",
        scope: dict[str, Any] | None = None,
        selected_ids: list[str] | None = None,
        dry_run: bool = False,
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = memory.start_workflow_run(
            workflow_id=workflow_id,
            intent=intent,
            query=query,
            request_id=request_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor=actor,
            scope=scope,
            selected_ids=selected_ids,
            dry_run=dry_run,
            source=source,
            metadata=metadata,
        )
        return _workflow_envelope(result, next_actions=["workflow_transition to executing after automatic preflight"])

    @server.tool(
        name="workflow_status",
        description="Read one compact workflow state, checkpoint, events, and external connector step references.",
    )
    def workflow_status_tool(
        run_id: int,
        include_events: bool = False,
        include_external_steps: bool = True,
    ) -> dict[str, Any]:
        result = memory.get_manager_run(
            run_id,
            include_events=include_events,
            include_external_steps=include_external_steps,
        )
        if result.get("ok"):
            item = result.get("item", {})
            return agent_envelope(
                ok=True,
                status=str(item.get("status") or "completed"),
                run_id=run_id,
                summary=item,
            )
        return _workflow_envelope(result)

    @server.tool(
        name="workflow_transition",
        description=(
            "Advance a workflow through planned, executing, external_wait, verifying, compensating, and terminal states "
            "using strict transitions and expected_state_version compare-and-swap. Completed requires positive evidence "
            "and rejects explicit executor or verification failure markers."
        ),
    )
    def workflow_transition_tool(
        run_id: int,
        status: str,
        message: str = "",
        verification: dict[str, Any] | None = None,
        summary: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            memory.transition_workflow_run(
                run_id,
                status=status,
                message=message,
                verification=verification,
                summary=summary,
                expected_state_version=expected_state_version,
            )
        )

    @server.tool(
        name="workflow_checkpoint",
        description=(
            "Persist a compact resumable checkpoint and selected IDs with expected_state_version compare-and-swap. "
            "Raw CRM dumps and email bodies are rejected."
        ),
    )
    def workflow_checkpoint_tool(
        run_id: int,
        checkpoint: dict[str, Any],
        selected_ids: list[str] | None = None,
        message: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            memory.checkpoint_workflow_run(
                run_id,
                checkpoint=checkpoint,
                selected_ids=selected_ids,
                message=message,
                expected_state_version=expected_state_version,
            )
        )

    @server.tool(
        name="workflow_wait_for_external",
        description=(
            "Register a refs-only step for a separate connector such as Gmail or Telegram and move the workflow to external_wait. "
            "Use expected_state_version compare-and-swap; message bodies, snippets, and raw content are rejected."
        ),
    )
    def workflow_wait_for_external_tool(
        run_id: int,
        step_id: str,
        connector: str,
        action: str,
        request_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            memory.register_external_step(
                run_id,
                step_id=step_id,
                connector=connector,
                action=action,
                request_refs=request_refs,
                expected_state_version=expected_state_version,
            ),
            next_actions=["call the separate connector", "complete_external_step with result IDs only"],
        )

    @server.tool(
        name="complete_external_step",
        description=(
            "Complete one external connector step with message/thread/draft/attachment/file IDs and timestamps only. "
            "Use expected_state_version compare-and-swap and never store raw Gmail or Telegram content in the manager ledger."
        ),
    )
    def complete_external_step_tool(
        run_id: int,
        step_id: str,
        result_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            memory.complete_external_step(
                run_id,
                step_id=step_id,
                result_refs=result_refs,
                expected_state_version=expected_state_version,
            ),
            next_actions=["workflow_resume after all external steps are complete"],
        )

    @server.tool(
        name="workflow_resume",
        description=(
            "Resume a planned or externally-waiting workflow from its compact checkpoint with expected_state_version "
            "compare-and-swap; refuses while external steps remain pending."
        ),
    )
    def workflow_resume_tool(run_id: int, expected_state_version: int | None = None) -> dict[str, Any]:
        return _workflow_envelope(memory.resume_workflow_run(run_id, expected_state_version=expected_state_version))

    @server.tool(
        name="workflow_cancel",
        description=(
            "Cancel a non-terminal workflow with expected_state_version compare-and-swap without changing CRM, Gmail, or Telegram state."
        ),
    )
    def workflow_cancel_tool(
        run_id: int,
        reason: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            memory.cancel_workflow_run(
                run_id,
                reason=reason,
                expected_state_version=expected_state_version,
            )
        )

    @server.tool(
        name="lookup_original_parts",
        description=(
            "Build a VIN, chassis, or market-code OEM lookup dossier with catalog routes, OEM candidates, confidence, and missing context."
        ),
    )
    def lookup_original_parts_tool(
        identifier: str,
        model_year: int | None = None,
        make_hint: str | None = None,
        part_name: str | None = None,
        part_group: str | None = None,
        side: str | None = None,
        position: str | None = None,
        old_part_number: str | None = None,
        captured_oem_number: str | None = None,
        captured_source: str | None = None,
        captured_supersedes: str | None = None,
        captured_note: str | None = None,
    ) -> dict[str, Any]:
        return lookup_original_parts(
            identifier,
            model_year=model_year,
            make_hint=make_hint,
            part_name=part_name,
            part_group=part_group,
            side=side,
            position=position,
            old_part_number=old_part_number,
            captured_oem_number=captured_oem_number,
            captured_source=captured_source,
            captured_supersedes=captured_supersedes,
            captured_note=captured_note,
        )

    @server.tool(
        name="estimate_repair_work_cost",
        description=(
            "Build a read-only multi-source labor estimate from aggregate-only closed repair-order experience, "
            "public Russia STO labor-only prices, exact vehicle context, and norm-hours/labor-time plausibility. "
            "Returns evidence families, a reconciled recommendation, confidence, gaps, and next actions without writes."
        ),
    )
    def estimate_repair_work_cost_tool(
        vehicle: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        make: str | None = None,
        model: str | None = None,
        year: int | str | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        work_items: str | list[str] | None = None,
        complaint: str | None = None,
        city: str = "Красноярск",
        quotes_json: list[dict[str, Any]] | dict[str, Any] | None = None,
        auto_research: bool = True,
        labor_time_policy: str = "public_only",
        use_internal_experience: bool = True,
    ) -> dict[str, Any]:
        return estimate_repair_work_cost(
            vehicle=vehicle,
            vin=vin,
            chassis=chassis,
            make=make,
            model=model,
            year=year,
            engine=engine,
            transmission=transmission,
            work_items=work_items,
            complaint=complaint,
            city=city,
            quotes_json=quotes_json,
            auto_research=auto_research,
            labor_time_policy=labor_time_policy,
            use_internal_experience=use_internal_experience,
        )

    @server.tool(
        name="decode_vehicle_identity",
        description=(
            "Build a source-aware vehicle identity dossier from a VIN/frame/body number: "
            "classification, check digit/model-year diagnostics, vPIC/WMI/platform evidence, "
            "CRM-context conflicts, confidence, and required EPC/API sources for parts lookup."
        ),
        annotations=ToolAnnotations(
            title="Vehicle Identity",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )
    def decode_vehicle_identity_tool(
        identifier: str,
        vehicle: str | None = None,
        make: str | None = None,
        model: str | None = None,
        model_year: int | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        drivetrain: str | None = None,
        market: str | None = None,
        source_confidence: float | None = None,
        live_vpic: bool = True,
        live_wmi: bool = True,
    ) -> dict[str, Any]:
        return decode_vehicle_identity(
            identifier,
            crm_context={
                "vehicle": vehicle,
                "make": make,
                "model": model,
                "model_year": model_year,
                "engine": engine,
                "transmission": transmission,
                "drivetrain": drivetrain,
                "market": market,
                "source_confidence": source_confidence,
            },
            model_year=model_year,
            make_hint=make,
            live_vpic=live_vpic,
            live_wmi=live_wmi,
        )

    server.tool(
        name="decode_vehicle_identities",
        description=(
            "Batch vehicle identity dossiers for VIN/frame/body-number lists. "
            "Returns per-identifier confidence, conflicts, adapter status, and required next EPC/API sources."
        ),
    )(decode_vehicle_identities)

    server.tool(
        name="catalog_provider_status",
        description=(
            "Report configured VIN/OEM/cross/procurement provider readiness without exposing secret values. "
            "Use before claiming live catalog or supplier API access."
        ),
    )(catalog_provider_status)

    server.tool(
        name="plan_oem_parts_providers",
        description=(
            "Build provider readiness and blocker plan for VIN/frame -> OEM candidates -> crosses/applicability "
            "-> procurement/RF market price. Does not call suppliers or write CRM."
        ),
    )(build_oem_parts_provider_plan)

    @server.tool(
        name="vin17_decode_vehicle",
        description=(
            "Call or dry-run the configured 17VIN API vehicle decoder. Requires VIN17_ACCOUNT/VIN17_SECRET; "
            "returns redacted request evidence and never exposes the token or secret."
        ),
    )
    def vin17_decode_vehicle_tool(identifier: str, dry_run: bool = False) -> dict[str, Any]:
        return vin17_decode_vehicle(identifier, dry_run=dry_run)

    @server.tool(
        name="vin17_search_part_number_by_vin",
        description=(
            "Call or dry-run 17VIN search_part_number by VIN after a 17VIN decode returns an EPC code. "
            "Use only for read-only fitment checks; no supplier order is created."
        ),
    )
    def vin17_search_part_number_by_vin_tool(
        identifier: str,
        epc: str,
        query_part_number: str,
        query_match_type: str = "exact",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return vin17_search_part_number_by_vin(
            epc=epc,
            identifier=identifier,
            query_part_number=query_part_number,
            query_match_type=query_match_type,
            dry_run=dry_run,
        )

    @server.tool(
        name="partsapi_catalog_lookup",
        description=(
            "Call or dry-run PartsAPI VIN/plate/OE/applicability/cross/part-name/AUTONORMS lookup. Live calls require PARTSAPI_BASE_URL plus "
            "PARTSAPI_KEY or a method-specific PARTSAPI_*_KEY; supports VINdecode, VINdecodeOE, getPartsbyVIN, "
            "getOEApplicability, getCrosses, getCrossesWithBrand, getCrossesTitle, getArticleCrosses, searchArticles, getEngine, "
            "gosnomer2vin, getPartnameByBrandNumber, and GetNormsMakes/GetNormsModels/GetNormsMotors/GetNormsTimes/GetFillVolumes. For getPartsbyVIN, "
            "part_type defaults to oem; use omit/non-oem to skip the type query parameter."
        ),
        annotations=ToolAnnotations(
            title="PartsAPI Catalog Lookup",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )
    def partsapi_catalog_lookup_tool(
        operation: str,
        identifier: str | None = None,
        registration_number: str | None = None,
        part_number: str | None = None,
        article_id: str | int | None = None,
        brand: str | None = None,
        part_type: str | None = None,
        category: str | None = None,
        vehicle_type: str | None = None,
        type_id: str | None = None,
        lang: str | None = None,
        lang_id: int | None = None,
        make_name_seo: str | None = None,
        model_id: str | int | None = None,
        motor_id: str | int | None = None,
        top_category_id: str | int | None = None,
        sub_category_id: str | int | None = None,
        car_id: str | int | None = None,
        timeout: float = 20.0,
        max_attempts: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return partsapi_catalog_lookup(
            operation=operation,
            identifier=identifier,
            registration_number=registration_number,
            part_number=part_number,
            article_id=article_id,
            brand=brand,
            part_type=part_type,
            category=category,
            vehicle_type=vehicle_type,
            type_id=type_id,
            lang=lang,
            lang_id=lang_id,
            make_name_seo=make_name_seo,
            model_id=model_id,
            motor_id=motor_id,
            top_category_id=top_category_id,
            sub_category_id=sub_category_id,
            car_id=car_id,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=dry_run,
        )

    server.tool(
        name="search_partsapi_category_index",
        description="Search the local PartsAPI numeric category index by query/intent without live calls or secrets.",
    )(search_partsapi_category_index)

    server.tool(
        name="explain_partsapi_category_for_intent",
        description="Explain why a PartsAPI numeric category was selected for a part intent.",
    )(explain_partsapi_category_for_intent)

    server.tool(
        name="validate_partsapi_category_index",
        description="Validate the tracked local PartsAPI category index fixture without exposing secrets or identifiers.",
    )(validate_partsapi_category_index)

    @server.tool(
        name="public_aftermarket_catalog_lookup",
        description=(
            "Call public aftermarket catalogs by part/OE number. Supports MANN-FILTER and DENSO live public endpoints; "
            "use as catalog enrichment, not as VIN-specific OEM EPC proof or procurement pricing."
        ),
    )
    def public_aftermarket_catalog_lookup_tool(
        provider: str,
        part_number: str,
        page_size: int = 5,
        country: str = "europe",
        include_detail: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return public_aftermarket_catalog_lookup(
            provider=provider,
            part_number=part_number,
            page_size=page_size,
            country=country,
            include_detail=include_detail,
            dry_run=dry_run,
        )

    @server.tool(
        name="exist_price_lookup",
        description=(
            "Call or dry-run public read-only Exist article lookup for catalog disambiguation, analog visibility, "
            "retail price benchmark, and lead time. Uses office 905 by default; returns public_retail_reference only."
        ),
    )
    def exist_price_lookup_tool(
        part_number: str,
        brand: str | None = None,
        pid: str | None = None,
        office_id: int = 905,
        max_candidates: int = 5,
        max_offers: int = 10,
        include_more_offers: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return exist_price_lookup(
            part_number=part_number,
            brand=brand,
            pid=pid,
            office_id=office_id,
            max_candidates=max_candidates,
            max_offers=max_offers,
            include_more_offers=include_more_offers,
            dry_run=dry_run,
        )

    server.tool(
        name="resolve_vin_oem_parts",
        description=(
            "Resolve one VIN/frame/body-number and requested part into a read-only VinOemResolution: "
            "identity, part intent, PartsAPI category, OEM candidates, enrichment, readiness gates, manual actions, and CRM gate."
        ),
        annotations=ToolAnnotations(
            title="OEM Catalog Candidates",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )(resolve_vin_oem_parts)

    server.tool(
        name="benchmark_vin_parts_lookup",
        description=(
            "Read-only benchmark for a batch of CRM VIN/frame/body-number items: identity confidence, part-intent recognition, "
            "safe public search templates, provider blockers, and PartsAPI/17VIN dry-run readiness. Raw identifiers are redacted from output."
        ),
    )(benchmark_vin_parts_lookup)

    server.tool(
        name="recommend_automotive_sources",
        description=(
            "Recommend authoritative repair, TSB, recall, diagnostic, wiring, labor, fluid, torque, or OEM source routes "
            "by brand and data type without copying licensed source content."
        ),
    )(recommend_automotive_sources)

    server.tool(
        name="lookup_public_automotive_evidence",
        description=(
            "Read compact official public automotive evidence: NHTSA model-level recalls, optional manufacturer-"
            "communications/TSB metadata, and applicable Mercedes/ZF fluid-reference routes. Does not use a VIN "
            "for campaign status, copy manuals, write CRM, or replace OEM service documentation."
        ),
        annotations=ToolAnnotations(
            title="Public Automotive Evidence",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )(lookup_public_automotive_evidence)

    if include_tools is not None:
        server.tool = original_tool
