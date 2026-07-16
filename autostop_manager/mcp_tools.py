from __future__ import annotations

from typing import Any

from .action_contract import prepare_action_contract
from .agent_gateway import agent_envelope, build_agent_bootstrap, list_agent_workflows
from .catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from .catalog_clients import (
    exist_price_lookup,
    lookup_oem_catalog_candidates,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    vin17_decode_vehicle,
    vin17_search_part_number_by_vin,
)
from .cleanup_audit import build_cleanup_audit
from .config import get_store_api_url, get_store_read_token
from .control_center import build_control_report, format_control_report_markdown
from .context import build_agent_brief, prepare_manager_context
from .crm_card_action import prepare_crm_card_action
from .crm_vin_parts import build_crm_vin_parts_lookup_pipeline
from .crm_health import build_crm_health_plan
from .fluid_maintenance import build_fluid_maintenance_plan
from .knowledge_base import (
    audit_knowledge_annotations,
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from .knowledge_intake import build_knowledge_intake_plan
from .memory_curator import audit_memory, curate_memory
from .memory_review import apply_memory_review_item, build_memory_review
from .partsapi_category_index import (
    explain_partsapi_category_for_intent,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)
from .service_management import build_service_management_plan
from .provider_smoke import build_provider_smoke_report
from .skill_registry import audit_skill_registry
from .source_catalog import recommend_automotive_sources
from .storage import ManagerMemoryStore
from .store_analytics import get_store_analytics_report
from .system_audit import build_system_audit
from .vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from .vin_parts_benchmark import benchmark_vin_parts_lookup
from .vin_parts_work_order import build_vin_parts_work_order
from .vin_oem_resolver import resolve_vin_oem_parts
from .vin_lookup import lookup_original_parts
from .work_pricing import estimate_repair_work_cost


def _registered_tool_names(server: Any) -> list[str] | None:
    tools = getattr(server, "tools", None)
    if isinstance(tools, dict):
        return sorted(str(name) for name in tools)
    return None


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
def register_manager_memory_tools(server: Any, store: ManagerMemoryStore | None = None) -> None:  # noqa: C901
    memory = store or ManagerMemoryStore()

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
        description="Return compact counts and sections for manager memory so the agent can navigate memory before broad recall.",
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
        description=(
            "Build compact memory context for a task: relevant preferences, rules, lessons, source boundaries, and suggested use. "
            "Use as context for judgment, not as a rigid text template."
        ),
    )
    def memory_context_for(task: str, limit: int = 5) -> dict[str, Any]:
        return memory.memory_context_for(task, limit=limit)

    @server.tool(
        name="memory_gaps",
        description="Return sparse or empty memory areas and review prompts without copying CRM or Gmail data.",
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
        description="Return manager memory context for today's work: due tasks, due reminders, recent journal, rules, and reusable routing context.",
    )
    def today_context(limit: int = 20) -> dict[str, Any]:
        return memory.today_context(limit=limit)

    @server.tool(
        name="prepare_manager_context",
        description=(
            "Prepare task-specific context by combining owner command routes, relevant memory/rules, "
            "knowledge-base routing, missing required context, and next actions."
        ),
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
            "Return a compact startup package for an agent before broad document reads: role, route, source boundaries, hot rules, "
            "read order, allowed/forbidden actions, missing context, next actions, and verification."
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
            "Return the compact Agent Gateway v2 startup envelope: deterministic workflow, source boundaries, "
            "internal safety policy, missing context, and resumable unfinished runs. It never reads or writes live CRM/Gmail."
        ),
    )
    def agent_bootstrap_tool(
        query: str = "",
        intent: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        return build_agent_bootstrap(memory, query=query, intent=intent, limit=limit)

    @server.tool(
        name="list_agent_workflows",
        description=(
            "List the compact named Codex workflow registry and resolve a query/intent without exposing raw CRM or Gmail data."
        ),
    )
    def list_agent_workflows_tool(
        query: str = "",
        intent: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return list_agent_workflows(query=query, intent=intent, limit=limit)

    @server.tool(
        name="get_store_analytics_report",
        description=(
            "Read a compact aggregate-only first-party AutoStop storefront report for today, yesterday, "
            "the last 7/30 days, or a custom Krasnoyarsk date range. Accepts a natural Russian query and "
            "never returns raw events, visitor/session identifiers, search text, or customer data."
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
        name="prepare_action_contract",
        description=(
            "Build a connector-neutral ActionContractV2 for CRM, finance, inventory, documents, files, or Gmail writes. "
            "Requires task intent, exact target where applicable, idempotency, concurrency, automatic preflight, compensation, "
            "and readback verification; never performs the write."
        ),
    )
    def prepare_action_contract_tool(
        domain: str,
        action: str,
        target_id: str = "",
        planned_changes: dict[str, Any] | None = None,
        owner_intent: str = "",
        expected_revision: str | None = None,
        idempotency_key: str = "",
        run_id: int | None = None,
        actor: str = "codex-owner-agent",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return prepare_action_contract(
            domain=domain,
            action=action,
            target_id=target_id,
            planned_changes=planned_changes,
            owner_intent=owner_intent,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            run_id=run_id,
            actor=actor,
            dry_run=dry_run,
        )

    @server.tool(
        name="prepare_crm_card_action",
        description=(
            "Build a dry-run CRM card edit contract for AutoStopManager: exact description patch, vehicle_profile patch "
            "with manual_fields preservation, board_summary target, expected_updated_at, tool sequence, ledger schema, "
            "risk flags, and strict reread verification spec. This tool never writes CRM."
        ),
    )
    def prepare_crm_card_action_tool(
        card_id: str,
        expected_updated_at: str | None = None,
        description: str | None = None,
        vehicle_profile: dict[str, Any] | None = None,
        board_summary: str | None = None,
        target_fields: list[str] | None = None,
        current_card: dict[str, Any] | None = None,
        intent: str = "board_cleanup",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return prepare_crm_card_action(
            card_id=card_id,
            expected_updated_at=expected_updated_at,
            description=description,
            vehicle_profile=vehicle_profile,
            board_summary=board_summary,
            target_fields=target_fields,
            current_card=current_card,
            intent=intent,
            dry_run=dry_run,
        )

    @server.tool(
        name="manager_journal",
        description="Append a short manager journal entry after important decisions, source changes, file intake, or CRM work.",
    )
    def manager_journal(
        event: str,
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
            "Audit docs/agent/knowledge_map.json, compact route cards, mapped source files, and SQLite index counts. "
            "Use after knowledge intake or when local knowledge routing looks stale."
        ),
    )
    def audit_knowledge_base_tool() -> dict[str, Any]:
        return audit_knowledge_base(memory)

    @server.tool(
        name="audit_knowledge_annotations",
        description="Audit compact knowledge annotations that improve fast routing before broad section reads.",
    )
    def audit_knowledge_annotations_tool() -> dict[str, Any]:
        return audit_knowledge_annotations(memory)

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
        return build_system_audit(store=memory, registered_tool_names=_registered_tool_names(server))

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
        name="crm_health_plan",
        description="Build a read-only CRM health plan from already fetched board_context, board_review, and today_context payloads.",
    )
    def crm_health_plan_tool(
        board_context: dict[str, Any] | None = None,
        board_review: dict[str, Any] | None = None,
        today_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_crm_health_plan(
            board_context=board_context,
            board_review=board_review,
            today_context=today_context,
        )

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
        name="memory_review",
        description="Generate rule-based MemoryReviewItem proposals without copying raw CRM, email, or secret content.",
    )
    def memory_review_tool() -> dict[str, Any]:
        return build_memory_review(memory)

    @server.tool(
        name="memory_review_apply",
        description=(
            "Apply a MemoryReviewItem decision. Supports accept, reject, or archive_duplicate; never deletes source memory records."
        ),
    )
    def memory_review_apply_tool(item_id: str, action: str) -> dict[str, Any]:
        return apply_memory_review_item(item_id, action, store=memory)

    @server.tool(
        name="knowledge_intake_plan",
        description=(
            "Classify a source file into KnowledgeIntakeDraft with safety flags and target metadata updates. "
            "Apply mode is review-gated and does not commit raw private files."
        ),
    )
    def knowledge_intake_plan_tool(path: str, apply: bool = False) -> dict[str, Any]:
        return build_knowledge_intake_plan(path, apply=apply)

    @server.tool(
        name="provider_smoke_report",
        description=(
            "Run safe ProviderSmokeResult readiness checks for one provider or all providers. "
            "Dry-run and live-readonly modes never call order, basket, or CRM writeback endpoints."
        ),
    )
    def provider_smoke_report_tool(provider: str = "all", mode: str = "dry-run") -> dict[str, Any]:
        return build_provider_smoke_report(provider=provider, mode=mode)

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
            "Register a refs-only step for a separate connector such as Gmail and move the workflow to external_wait. "
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
            "Use expected_state_version compare-and-swap and never store raw Gmail bodies in the manager ledger."
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
            "Cancel a non-terminal workflow with expected_state_version compare-and-swap without changing CRM or Gmail state."
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
            "Build a read-only labor cost estimate from vehicle/work items, public Russia STO labor-only prices, "
            "and a public norm-hours/labor-time plausibility layer. Returns Russia average, AutoStop +50% price, "
            "labor-time checks, confidence, missing context, and next actions without writing repair orders."
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
        )

    @server.tool(
        name="decode_vehicle_identity",
        description=(
            "Build a source-aware vehicle identity dossier from a VIN/frame/body number: "
            "classification, check digit/model-year diagnostics, vPIC/WMI/platform evidence, "
            "CRM-context conflicts, confidence, and required EPC/API sources for parts lookup."
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

    @server.tool(
        name="decode_vehicle_identities",
        description=(
            "Batch vehicle identity dossiers for VIN/frame/body-number lists. "
            "Returns per-identifier confidence, conflicts, adapter status, and required next EPC/API sources."
        ),
    )
    def decode_vehicle_identities_tool(
        items: list[dict[str, Any]],
        live_vpic: bool = True,
        use_vpic_batch: bool = True,
    ) -> dict[str, Any]:
        return decode_vehicle_identities(items, live_vpic=live_vpic, use_vpic_batch=use_vpic_batch)

    @server.tool(
        name="catalog_provider_status",
        description=(
            "Report configured VIN/OEM/cross/procurement provider readiness without exposing secret values. "
            "Use before claiming live catalog or supplier API access."
        ),
    )
    def catalog_provider_status_tool(stage: str | None = None) -> dict[str, Any]:
        return catalog_provider_status(stage=stage)

    @server.tool(
        name="plan_oem_parts_providers",
        description=(
            "Build provider readiness and blocker plan for VIN/frame -> OEM candidates -> crosses/applicability "
            "-> procurement/RF market price. Does not call suppliers or write CRM."
        ),
    )
    def plan_oem_parts_providers_tool(
        identifier: str,
        requested_part: str,
        vehicle_identity: dict[str, Any] | None = None,
        city: str = "Красноярск",
    ) -> dict[str, Any]:
        return build_oem_parts_provider_plan(
            identifier=identifier,
            requested_part=requested_part,
            vehicle_identity=vehicle_identity,
            city=city,
        )

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
            "Call or dry-run PartsAPI VIN/OE/applicability/cross lookup. Live calls require PARTSAPI_BASE_URL plus "
            "PARTSAPI_KEY or a method-specific PARTSAPI_*_KEY; supports VINdecode, VINdecodeOE, getPartsbyVIN, "
            "getOEApplicability, getCrosses, getCrossesWithBrand, getCrossesTitle, getArticleCrosses, searchArticles, and getEngine. For getPartsbyVIN, "
            "part_type defaults to oem; use omit/non-oem to skip the type query parameter."
        ),
    )
    def partsapi_catalog_lookup_tool(
        operation: str,
        identifier: str | None = None,
        part_number: str | None = None,
        article_id: str | int | None = None,
        brand: str | None = None,
        part_type: str | None = None,
        category: str | None = None,
        vehicle_type: str | None = None,
        type_id: str | None = None,
        lang: str | None = None,
        lang_id: int | None = None,
        timeout: float = 20.0,
        max_attempts: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return partsapi_catalog_lookup(
            operation=operation,
            identifier=identifier,
            part_number=part_number,
            article_id=article_id,
            brand=brand,
            part_type=part_type,
            category=category,
            vehicle_type=vehicle_type,
            type_id=type_id,
            lang=lang,
            lang_id=lang_id,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=dry_run,
        )

    @server.tool(
        name="search_partsapi_category_index",
        description="Search the local PartsAPI numeric category index by query/intent without live calls or secrets.",
    )
    def search_partsapi_category_index_tool(
        query: str,
        intent_id: str | None = None,
        path: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        return search_partsapi_category_index(query, intent_id=intent_id, path=path, limit=limit)

    @server.tool(
        name="explain_partsapi_category_for_intent",
        description="Explain why a PartsAPI numeric category was selected for a part intent.",
    )
    def explain_partsapi_category_for_intent_tool(
        intent_id: str,
        query: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        return explain_partsapi_category_for_intent(intent_id, query=query, path=path)

    @server.tool(
        name="validate_partsapi_category_index",
        description="Validate the tracked local PartsAPI category index fixture without exposing secrets or identifiers.",
    )
    def validate_partsapi_category_index_tool(path: str | None = None) -> dict[str, Any]:
        return validate_partsapi_category_index(path=path)

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

    @server.tool(
        name="lookup_oem_catalog_candidates",
        description=(
            "Call or dry-run the multi-provider OEM candidate lookup for one VIN/frame and requested part. "
            "Combines Parts-Catalogs, PartsAPI, and 17VIN when their credentials and routing ids are available; no CRM writes or orders are created."
        ),
    )
    def lookup_oem_catalog_candidates_tool(
        identifier: str,
        requested_part: str,
        catalog_id: str | None = None,
        car_id: str | None = None,
        group_id: str | None = None,
        epc: str | None = None,
        partsapi_part_type: str = "oem",
        partsapi_category: str | None = None,
        partsapi_category_index_path: str | None = None,
        timeout: float = 20.0,
        max_attempts: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return lookup_oem_catalog_candidates(
            identifier=identifier,
            requested_part=requested_part,
            catalog_id=catalog_id,
            car_id=car_id,
            group_id=group_id,
            epc=epc,
            partsapi_part_type=partsapi_part_type,
            partsapi_category=partsapi_category,
            partsapi_category_index_path=partsapi_category_index_path,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=dry_run,
        )

    @server.tool(
        name="resolve_vin_oem_parts",
        description=(
            "Resolve one VIN/frame/body-number and requested part into a read-only VinOemResolution: "
            "identity, part intent, PartsAPI category, OEM candidates, enrichment, readiness gates, manual actions, and CRM gate."
        ),
    )
    def resolve_vin_oem_parts_tool(
        identifier: str,
        requested_part: str,
        make: str | None = None,
        model: str | None = None,
        model_year: int | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        market: str | None = None,
        drivetrain: str | None = None,
        axle: str | None = None,
        side: str | None = None,
        position: str | None = None,
        live_vpic: bool = True,
        live_partsapi_identity: bool = False,
        live_partsapi_oem: bool = False,
        max_live_calls: int = 3,
        max_candidates: int = 3,
        timeout: float = 20.0,
        max_attempts: int = 1,
        partsapi_category_index: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return resolve_vin_oem_parts(
            identifier=identifier,
            requested_part=requested_part,
            make=make,
            model=model,
            model_year=model_year,
            engine=engine,
            transmission=transmission,
            market=market,
            drivetrain=drivetrain,
            axle=axle,
            side=side,
            position=position,
            live_vpic=live_vpic,
            live_partsapi_identity=live_partsapi_identity,
            live_partsapi_oem=live_partsapi_oem,
            max_live_calls=max_live_calls,
            max_candidates=max_candidates,
            timeout=timeout,
            max_attempts=max_attempts,
            partsapi_category_index=partsapi_category_index,
            dry_run=dry_run,
        )

    @server.tool(
        name="plan_crm_vin_oem_parts_lookup",
        description=(
            "Build the CRM card workflow for VIN/frame/body-number OEM lookup, replacements/crosses, "
            "procurement/RF market pricing, structured CRM writeback, and verification."
        ),
    )
    def plan_crm_vin_oem_parts_lookup_tool(
        card_id: str | None = None,
        requested_part: str | None = None,
        vin: str | None = None,
        frame: str | None = None,
        body_number: str | None = None,
        vehicle: str | None = None,
        make: str | None = None,
        model: str | None = None,
        model_year: int | None = None,
        market: str | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        drivetrain: str | None = None,
        side: str | None = None,
        axle: str | None = None,
        position: str | None = None,
        urgency: str | None = None,
        city: str = "Красноярск",
        limit: int = 10,
        vin_oem_resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_crm_vin_parts_lookup_pipeline(
            card_id=card_id,
            requested_part=requested_part,
            vin=vin,
            frame=frame,
            body_number=body_number,
            vehicle=vehicle,
            make=make,
            model=model,
            model_year=model_year,
            market=market,
            engine=engine,
            transmission=transmission,
            drivetrain=drivetrain,
            side=side,
            axle=axle,
            position=position,
            urgency=urgency,
            city=city,
            limit=limit,
            vin_oem_resolution=vin_oem_resolution,
        )

    @server.tool(
        name="benchmark_vin_parts_lookup",
        description=(
            "Read-only benchmark for a batch of CRM VIN/frame/body-number items: identity confidence, part-intent recognition, "
            "safe public search templates, provider blockers, and PartsAPI/17VIN dry-run readiness. Raw identifiers are redacted from output."
        ),
    )
    def benchmark_vin_parts_lookup_tool(
        items: list[dict[str, Any]],
        requested_part: str,
        city: str = "Красноярск",
        live_vpic: bool = True,
        use_vpic_batch: bool = True,
        include_partsapi_dry_run: bool = True,
        include_vin17_dry_run: bool = True,
        include_oem_catalog_dry_run: bool = True,
        live_partsapi_identity: bool = False,
        live_partsapi_oem: bool = False,
        resolve_oem: bool = False,
        max_live_calls: int = 3,
        max_candidates: int = 3,
        partsapi_category_index: str | None = None,
        partsapi_timeout: float = 20.0,
    ) -> dict[str, Any]:
        return benchmark_vin_parts_lookup(
            items,
            requested_part=requested_part,
            city=city,
            live_vpic=live_vpic,
            use_vpic_batch=use_vpic_batch,
            include_partsapi_dry_run=include_partsapi_dry_run,
            include_vin17_dry_run=include_vin17_dry_run,
            include_oem_catalog_dry_run=include_oem_catalog_dry_run,
            live_partsapi_identity=live_partsapi_identity,
            live_partsapi_oem=live_partsapi_oem,
            resolve_oem=resolve_oem,
            max_live_calls=max_live_calls,
            max_candidates=max_candidates,
            partsapi_category_index=partsapi_category_index,
            partsapi_timeout=partsapi_timeout,
        )

    @server.tool(
        name="build_vin_parts_work_order",
        description=(
            "Build read-only per-card VIN/frame parts lookup work orders: exact OEM/EPC routes, prepared API checks, "
            "cross/applicability steps, supplier routes, CRM writeback gates, blockers, and acceptance checklists. "
            "Raw identifiers are redacted from output."
        ),
    )
    def build_vin_parts_work_order_tool(
        items: list[dict[str, Any]],
        requested_part: str,
        city: str = "Красноярск",
        live_vpic: bool = True,
        use_vpic_batch: bool = True,
        live_partsapi_identity: bool = False,
        live_partsapi_oem: bool = False,
        resolve_oem: bool = False,
        max_live_calls: int = 3,
        max_candidates: int = 3,
        partsapi_category_index: str | None = None,
    ) -> dict[str, Any]:
        return build_vin_parts_work_order(
            items,
            requested_part=requested_part,
            city=city,
            live_vpic=live_vpic,
            use_vpic_batch=use_vpic_batch,
            live_partsapi_identity=live_partsapi_identity,
            live_partsapi_oem=live_partsapi_oem,
            resolve_oem=resolve_oem,
            max_live_calls=max_live_calls,
            max_candidates=max_candidates,
            partsapi_category_index=partsapi_category_index,
        )

    @server.tool(
        name="recommend_automotive_sources",
        description=(
            "Recommend authoritative repair, TSB, recall, diagnostic, wiring, labor, fluid, torque, or OEM source routes "
            "by brand and data type without copying licensed source content."
        ),
    )
    def recommend_automotive_sources_tool(
        brand: str | None = None,
        data_type: str | None = None,
        include_licensed: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        return recommend_automotive_sources(
            brand=brand,
            data_type=data_type,
            include_licensed=include_licensed,
            limit=limit,
        )

    @server.tool(
        name="recommend_fluid_maintenance_sources",
        description=(
            "Build a source-backed plan for oils, operating fluids, fill capacities, and ТО fluid service by vehicle unit. "
            "Use before giving engine, transmission, differential, transfer case, brake, coolant, or steering fluid facts."
        ),
    )
    def recommend_fluid_maintenance_sources_tool(
        brand: str | None = None,
        unit: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        model: str | None = None,
        year: int | None = None,
        engine_code: str | None = None,
        transmission_code: str | None = None,
        drivetrain: str | None = None,
        market: str | None = None,
        service_operation: str | None = None,
        unit_variant: str | None = None,
        fluid_spec: str | None = None,
        level_check_procedure: str | None = None,
        include_licensed: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        return build_fluid_maintenance_plan(
            brand=brand,
            unit=unit,
            vin=vin,
            chassis=chassis,
            model=model,
            year=year,
            engine_code=engine_code,
            transmission_code=transmission_code,
            drivetrain=drivetrain,
            market=market,
            service_operation=service_operation,
            unit_variant=unit_variant,
            fluid_spec=fluid_spec,
            level_check_procedure=level_check_procedure,
            include_licensed=include_licensed,
            limit=limit,
        )

    @server.tool(
        name="recommend_service_management_actions",
        description=(
            "Build a Krasnoyarsk AutoStop/Автоспорт workshop-management action plan for parts procurement, "
            "repair triage, staff load, customer flow, finance control, daily CRM control, or knowledge intake."
        ),
    )
    def recommend_service_management_actions_tool(
        area: str | None = None,
        city: str = "Красноярск",
        vehicle: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        part_number: str | None = None,
        part_name: str | None = None,
        urgency: str | None = None,
        role: str | None = None,
        complaint: str | None = None,
        dtc_or_scan: str | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        mileage: str | None = None,
        current_load: str | None = None,
        output_or_hours: str | None = None,
        quality_signal: str | None = None,
        card_id: str | None = None,
        client_contact: str | None = None,
        next_action: str | None = None,
        approval_status: str | None = None,
        repair_orders: str | None = None,
        cashbox: str | None = None,
        payment_status: str | None = None,
        file_path: str | None = None,
        source_type: str | None = None,
        license_status: str | None = None,
        target_playbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return build_service_management_plan(
            area=area,
            city=city,
            vehicle=vehicle,
            vin=vin,
            chassis=chassis,
            part_number=part_number,
            part_name=part_name,
            urgency=urgency,
            role=role,
            complaint=complaint,
            dtc_or_scan=dtc_or_scan,
            engine=engine,
            transmission=transmission,
            mileage=mileage,
            current_load=current_load,
            output_or_hours=output_or_hours,
            quality_signal=quality_signal,
            card_id=card_id,
            client_contact=client_contact,
            next_action=next_action,
            approval_status=approval_status,
            repair_orders=repair_orders,
            cashbox=cashbox,
            payment_status=payment_status,
            file_path=file_path,
            source_type=source_type,
            license_status=license_status,
            target_playbook=target_playbook,
            limit=limit,
        )
